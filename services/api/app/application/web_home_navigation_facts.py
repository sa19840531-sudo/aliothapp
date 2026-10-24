"""从同帧可信导航中识别规范根页的选中首页入口。"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit

from services.api.app.application.web_logical_page import is_web_home_url
from services.api.app.application.web_navigation_recognition import (
    WebNavigationAssessment,
    navigation_container_identity,
    navigation_entry_kind,
    navigation_is_outside_viewport,
    navigation_runtime_evidence_is_trusted,
)

_HOME_LABELS = frozenset({"首页", "主页", "home"})
_ACTIVE_TOKENS = frozenset(
    {"active", "is-active", "current", "is-current", "selected", "is-selected"}
)


def has_trusted_active_home_navigation(
    snapshot: Mapping[str, object],
    assessment: WebNavigationAssessment,
    *,
    current_url: str | None,
) -> bool:
    """仅接受无遮挡、同源根 URL 且唯一选中的可信首页导航。"""

    snapshot_url = snapshot.get("url")
    if (
        not isinstance(current_url, str)
        or not isinstance(snapshot_url, str)
        or not is_web_home_url(current_url)
        or not is_web_home_url(snapshot_url)
        or not assessment.proven
        or assessment.facts.navigation_obstructed
        or assessment.facts.visible_category_entry_count < 3
        or not _same_origin(current_url, snapshot_url)
    ):
        return False
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return False
    proven = set(assessment.proven_container_ids)
    matches = 0
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        if (
            element.get("visible") is not True
            or element.get("interactive") is not True
            or element.get("occluded") is True
            or element.get("obstructed") is True
            or navigation_container_identity(element) not in proven
            or navigation_entry_kind(element) != "category"
            or not navigation_runtime_evidence_is_trusted(element)
            or navigation_is_outside_viewport(element, snapshot)
        ):
            continue
        label = " ".join(
            str(element.get(key) or "").strip()
            for key in ("text", "aria_label")
            if element.get(key)
        ).casefold()
        if not any(part.strip() in _HOME_LABELS for part in label.split(" ")):
            continue
        tokens = element.get("class_tokens")
        aria_current = element.get("aria_current")
        aria_selected = element.get("aria_selected")
        selected = (
            bool(
                isinstance(tokens, list)
                and any(
                    isinstance(token, str) and token.casefold() in _ACTIVE_TOKENS
                    for token in tokens
                )
            )
            or aria_current is True
            or (isinstance(aria_current, str) and aria_current.casefold() in {"true", "page"})
            or aria_selected is True
            or (isinstance(aria_selected, str) and aria_selected.casefold() == "true")
        )
        if not selected:
            continue
        href = element.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        target = urljoin(snapshot_url, href)
        if not _same_origin(current_url, target) or not is_web_home_url(target):
            continue
        matches += 1
        if matches > 1:
            return False
    return matches == 1


def _same_origin(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        if a.scheme not in {"http", "https"} or b.scheme not in {"http", "https"}:
            return False
        return (a.scheme, a.hostname, a.port or (443 if a.scheme == "https" else 80)) == (
            b.scheme,
            b.hostname,
            b.port or (443 if b.scheme == "https" else 80),
        )
    except ValueError:
        return False
