"""Web 菜单与详情动作后最终 URL 的同站和 scheme 单调性验签。"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import SplitResult, urlsplit


class WebCaptureUrlReader(Protocol):
    async def read_url(self, *, capture_id: str) -> str: ...


class WebNavigationSafetyError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def validate_web_final_url(
    *,
    site_url: str,
    requested_url: str,
    final_url: str,
) -> None:
    """仅允许同站 HTTP(S) 导航，并禁止 HTTPS 降级或自定义端口漂移。"""

    site = _absolute_http_url(site_url)
    requested = _absolute_http_url(requested_url)
    final = _absolute_http_url(final_url)
    hostnames = {
        str(site.hostname).casefold(),
        str(requested.hostname).casefold(),
        str(final.hostname).casefold(),
    }
    if len(hostnames) != 1:
        raise WebNavigationSafetyError(
            "WEB_NAVIGATION_CROSS_ORIGIN",
            "动作后页面已跳转到非目标站点",
        )
    if final.scheme.casefold() == "http" and (
        requested.scheme.casefold() == "https"
        or site.scheme.casefold() == "https"
    ):
        raise WebNavigationSafetyError(
            "WEB_NAVIGATION_HTTPS_DOWNGRADE",
            "动作后页面发生 HTTPS 到 HTTP 降级",
        )
    if not _ports_are_safe(requested, final):
        raise WebNavigationSafetyError(
            "WEB_NAVIGATION_PORT_DRIFT",
            "动作后页面端口发生未核准变化",
        )


def _absolute_http_url(value: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (AttributeError, ValueError) as exc:
        raise WebNavigationSafetyError(
            "WEB_NAVIGATION_URL_INVALID",
            "导航 URL 无效",
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise WebNavigationSafetyError(
            "WEB_NAVIGATION_URL_INVALID",
            "导航 URL 必须为无凭据 HTTP(S) URL",
        )
    return parsed


def _ports_are_safe(requested: SplitResult, final: SplitResult) -> bool:
    requested_scheme = requested.scheme.casefold()
    final_scheme = final.scheme.casefold()
    requested_default = 443 if requested_scheme == "https" else 80
    final_default = 443 if final_scheme == "https" else 80
    requested_port = requested.port or requested_default
    final_port = final.port or final_default
    if requested_scheme == final_scheme:
        return requested_port == final_port
    return (
        requested_scheme == "http"
        and final_scheme == "https"
        and requested.port in {None, 80}
        and final.port in {None, 443}
    )


__all__ = [
    "WebCaptureUrlReader",
    "WebNavigationSafetyError",
    "validate_web_final_url",
]
