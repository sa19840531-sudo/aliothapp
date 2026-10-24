"""组合 Web URL、菜单与 DOM 分类提示；提示本身不具备定案权。"""

from __future__ import annotations

from dataclasses import dataclass

from services.api.app.domain.page.web_site_menu_registry import (
    WebCollectionEligibility,
    WebPageClassificationHint,
    WebSiteMenuCatalog,
    canonical_menu_url,
)


@dataclass(frozen=True, kw_only=True)
class ResolvedWebPageClassificationHint:
    hint: WebPageClassificationHint | None
    eligibility: WebCollectionEligibility
    entry_ids: tuple[str, ...]


def resolve_web_page_classification_hint(
    *,
    site: WebSiteMenuCatalog,
    url: str,
) -> ResolvedWebPageClassificationHint:
    """按规范 URL 汇总已审核条目；冲突时不猜测并转待复核。"""

    canonical_url = canonical_menu_url(url, site_origin=site.origin)
    if not site.contains_url_origin(canonical_url):
        return ResolvedWebPageClassificationHint(
            hint=None,
            eligibility=WebCollectionEligibility(eligible=False, reason="cross_origin_blocked"),
            entry_ids=(),
        )
    matches = tuple(
        entry for entry in site.entries if entry.canonical_url == canonical_url
    )
    if not matches:
        return ResolvedWebPageClassificationHint(
            hint=None,
            eligibility=WebCollectionEligibility(
                eligible=True,
                reason="runtime_url_not_in_reviewed_catalog",
            ),
            entry_ids=(),
        )
    eligible = tuple(entry for entry in matches if entry.collection_eligibility.eligible)
    if not eligible:
        return ResolvedWebPageClassificationHint(
            hint=None,
            eligibility=WebCollectionEligibility(
                eligible=False,
                reason=matches[0].collection_eligibility.reason,
            ),
            entry_ids=tuple(entry.entry_id for entry in matches),
        )
    hints = tuple(entry.classification_hint for entry in eligible)
    expected_types = {
        hint.expected_page_type for hint in hints if hint is not None
    }
    hint = (
        max(
            (item for item in hints if item is not None),
            key=lambda item: item.confidence,
        )
        if len(expected_types) == 1
        else None
    )
    return ResolvedWebPageClassificationHint(
        hint=hint,
        eligibility=WebCollectionEligibility(
            eligible=True,
            reason="conflicting_catalog_hints" if hint is None else None,
        ),
        entry_ids=tuple(entry.entry_id for entry in eligible),
    )


__all__ = [
    "ResolvedWebPageClassificationHint",
    "resolve_web_page_classification_hint",
]
