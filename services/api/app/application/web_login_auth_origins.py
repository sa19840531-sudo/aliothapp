"""Web 登录认证源站配置；只保存站点与源站，不保存凭据。"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from services.api.app.application.web_login_allowlist import (
    WebLoginAllowlist,
    WebLoginAllowlistError,
    _origin,
)

DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH = (
    Path(__file__).resolve().parents[4] / "infra" / "env" / "web-login-auth-origins.json"
)


class WebLoginAuthOriginsError(ValueError):
    """配置无效；错误不包含原始配置正文。"""


@dataclass(frozen=True)
class WebLoginAuthOrigins:
    _sites: dict[str, frozenset[str]] = field(repr=False)

    def for_site(self, origin: str) -> frozenset[str]:
        return self._sites.get(origin, frozenset())


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_DUPLICATE")
        result[key] = value
    return result


def parse_web_login_auth_origins(
    text: str, *, allowlist: WebLoginAllowlist
) -> WebLoginAuthOrigins:
    try:
        data = json.loads(text, object_pairs_hook=_unique_object)
    except (TypeError, ValueError):
        raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_CONFIG_INVALID") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "sites"}
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["sites"], dict)
    ):
        raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_CONFIG_INVALID")
    sites: dict[str, frozenset[str]] = {}
    for site, configured in data["sites"].items():
        if site not in allowlist.origins or not isinstance(configured, list):
            raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_SITE_INVALID")
        origins: set[str] = set()
        for value in configured:
            if not isinstance(value, str):
                raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_INVALID")
            parsed = None
            try:
                parsed = urlsplit(value)
                normalized = _origin(value, allow_http=False, allow_path=False)
                if normalized != value or parsed.path or parsed.query or parsed.fragment:
                    raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_INVALID")
                address = ipaddress.ip_address(parsed.hostname or "")
            except ValueError as exc:
                if isinstance(exc, WebLoginAuthOriginsError):
                    raise
                if isinstance(exc, WebLoginAllowlistError):
                    raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_INVALID") from None
                if parsed is None or (parsed.hostname or "").casefold() in {
                    "localhost", "localhost.localdomain"
                }:
                    raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_INVALID") from None
            else:
                if not address.is_global:
                    raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_INVALID")
            if value in origins:
                raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_ORIGIN_DUPLICATE")
            origins.add(value)
        sites[site] = frozenset(origins)
    return WebLoginAuthOrigins(sites)


def load_web_login_auth_origins(
    path: str | Path, *, allowlist: WebLoginAllowlist
) -> WebLoginAuthOrigins:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise WebLoginAuthOriginsError("WEB_LOGIN_AUTH_CONFIG_READ_FAILED") from None
    return parse_web_login_auth_origins(text, allowlist=allowlist)
