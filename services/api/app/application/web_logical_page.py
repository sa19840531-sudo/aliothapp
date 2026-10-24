"""Web 规范 URL、逻辑页面身份与 Capture 版本聚合。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)

_PRIMARY_CAPTURE_PURPOSE_PRIORITY = {
    "web_page_ready_baseline": 10,
    "web_popup_cleared_recapture": 20,
    "web_primary_content_evidence": 30,
}


@dataclass(frozen=True, kw_only=True)
class WebLogicalPage:
    """同一规范 URL 下可审计的 Capture 版本集合。"""

    logical_page_id: str
    canonical_url: str
    menu_entry_ids: tuple[str, ...]
    capture_ids: tuple[str, ...]
    primary_capture_id: str
    home_role_verified: bool = False


def canonical_web_logical_page_url(
    url: str, *, identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY
) -> str:
    """生成稳定的 Web 页面身份 URL，同时保留业务查询参数。"""

    return identity_policy.canonical(url)


def is_web_home_url(url: str) -> bool:
    """判断规范化后的 URL 是否为不带业务查询参数的站点首页。"""

    parsed = urlsplit(canonical_web_logical_page_url(url))
    return parsed.path == "/" and not parsed.query and not parsed.fragment


def resolve_web_logical_page_role(
    *,
    canonical_url: str,
    menu_entry_ids: Iterable[str] = (),
    asserted_roles: Iterable[object] = (),
) -> str | None:
    """从持久化评估、首页菜单身份和规范 URL 恢复逻辑页面角色。"""

    roles = tuple(
        normalized
        for value in asserted_roles
        if isinstance(value, str)
        and (normalized := value.strip().casefold()) in {"home", "menu", "detail"}
    )
    if is_web_home_url(canonical_url):
        return "home"
    if "detail" in roles:
        return "detail"
    if "menu" in roles:
        return "menu"
    return None


def web_logical_page_id(
    url: str, *, identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY
) -> str:
    """由规范 URL 生成不泄露原始地址的稳定逻辑页面 ID。"""

    return identity_policy.logical_id(url)


def build_web_logical_pages(
    captures: Iterable[Mapping[str, Any]],
    *, trusted_identities: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[WebLogicalPage, ...]:
    """按首次出现顺序聚合 Capture，并为每页选择当前主证据版本。"""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    canonical_urls: dict[str, str] = {}
    for capture in captures:
        capture_id = _optional_text(capture.get("page_capture_id") or capture.get("capture_id"))
        raw_url = _optional_text(capture.get("url") or capture.get("page_url"))
        if capture_id is None or raw_url is None:
            continue
        if trusted_identities is not None:
            identity = trusted_identities.get(capture_id, {})
            if identity.get("logical_page_identity_status") != "verified":
                continue
            logical_page_id = identity.get("logical_page_id")
            canonical_url = identity.get("canonical_url")
            if not logical_page_id or not canonical_url:
                continue
        else:
            try:
                canonical_url = canonical_web_logical_page_url(raw_url)
            except (TypeError, ValueError):
                continue
            logical_page_id = web_logical_page_id(canonical_url)
        groups.setdefault(logical_page_id, []).append(capture)
        canonical_urls[logical_page_id] = canonical_url

    pages: list[WebLogicalPage] = []
    for logical_page_id, versions in groups.items():
        capture_ids = tuple(
            capture_id
            for capture in versions
            if (capture_id := _optional_text(
                capture.get("page_capture_id") or capture.get("capture_id")
            ))
            is not None
        )
        menu_entry_ids = _ordered_unique(
            entry_id
            for capture in versions
            for entry_id in _capture_menu_entry_ids(capture)
        )
        primary = max(enumerate(versions), key=_primary_capture_sort_key)[1]
        primary_capture_id = _optional_text(
            primary.get("page_capture_id") or primary.get("capture_id")
        )
        if primary_capture_id is None:
            continue
        pages.append(
            WebLogicalPage(
                logical_page_id=logical_page_id,
                canonical_url=canonical_urls[logical_page_id],
                menu_entry_ids=menu_entry_ids,
                capture_ids=capture_ids,
                primary_capture_id=primary_capture_id,
                home_role_verified=bool(trusted_identities and any(trusted_identities.get(capture_id, {}).get("home_role_verified") for capture_id in capture_ids)),
            )
        )
    return tuple(pages)


def web_logical_page_display_url(url: str) -> str:
    """展示地址移除凭据、查询与片段；禁止用展示结果重新计算身份。"""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))


def _capture_menu_entry_ids(capture: Mapping[str, Any]) -> tuple[str, ...]:
    raw_ids = capture.get("menu_entry_ids")
    if isinstance(raw_ids, list | tuple):
        return tuple(
            entry_id
            for item in raw_ids
            if (entry_id := _optional_text(item)) is not None
        )
    entry_id = _optional_text(capture.get("menu_entry_id"))
    return (entry_id,) if entry_id is not None else ()


def _primary_capture_sort_key(
    indexed_capture: tuple[int, Mapping[str, Any]],
) -> tuple[int, int, int]:
    index, capture = indexed_capture
    purpose = (_optional_text(capture.get("capture_purpose")) or "").casefold()
    priority = _PRIMARY_CAPTURE_PURPOSE_PRIORITY.get(purpose, 0)
    page_seq = capture.get("page_seq") or capture.get("node_seq") or 0
    try:
        numeric_seq = int(page_seq)
    except (TypeError, ValueError):
        numeric_seq = 0
    return priority, numeric_seq, index


def _ordered_unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
