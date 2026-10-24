"""主文档可读事实独立于 HTTP 缓存状态与任务域边界。"""

from collections.abc import Mapping
from urllib.parse import urlsplit

from services.api.app.application.observation_surface_guard import (
    target_document_unavailability_reason,
)


def navigation_document_facts(
    *, final_url: str, status_code: int | None, readiness: Mapping[str, object] | None,
    title: str = "", visible_text: str = "",
) -> dict[str, object]:
    """只根据浏览器最终文档判断可用性，不把 304 等同于导航失败。"""
    parsed = urlsplit(final_url)
    readable = readiness is not None
    ready_state = readiness.get("document_ready_state") if readiness is not None else None
    blank = readiness.get("blank_document") if readiness is not None else None
    unavailable_reason = target_document_unavailability_reason(
        title=title, visible_text=visible_text,
    )
    error_code = None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        error_code = "WEB_NAVIGATION_DOCUMENT_UNAVAILABLE"
    elif status_code is not None and (status_code >= 400 or status_code in {204, 205}):
        error_code = "WEB_NAVIGATION_HTTP_ERROR"
    elif not readable or ready_state not in {"interactive", "complete"}:
        error_code = "WEB_NAVIGATION_DOCUMENT_UNREADABLE"
    elif blank is True:
        error_code = "WEB_NAVIGATION_BLANK_DOCUMENT"
    elif unavailable_reason is not None:
        error_code = "WEB_NAVIGATION_TARGET_DOCUMENT_UNAVAILABLE"
    return {
        "schema_version": "navigation_document.v1",
        "main_response_status": status_code,
        "document_readable": readable,
        "document_ready_state": ready_state,
        "blank_document": blank,
        "inspectable": error_code is None,
        "target_unavailability_reason": unavailable_reason,
        "error_code": error_code,
    }
