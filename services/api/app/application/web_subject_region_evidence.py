"""主体事实的有界审计摘要与可选几何证明。"""

from collections.abc import Mapping
from hashlib import sha256

from services.api.app.application.web_subject_region_scopes import (
    ARTICLE_TOKENS,
    GALLERY_TOKENS,
    RECOMMENDATION_TOKENS,
    VENDOR_TOKENS,
    SubjectRegions,
    media_dominates_subject,
    media_geometry_large_enough,
    tokens,
    valid_rect,
)

SEMANTIC_TOKENS = (
    ARTICLE_TOKENS
    | GALLERY_TOKENS
    | RECOMMENDATION_TOKENS
    | VENDOR_TOKENS
    | {
        "article",
        "main",
        "video",
        "source",
        "iframe",
        "p",
        "li",
        "a",
        "img",
        "breadcrumb",
        "breadcrumbs",
        "advertisement",
        "ad-slot",
        "sponsored",
        "native_media_resource",
        "playback_controls",
        "media_geometry_dominant",
        "paragraph_body",
        "repeated_cards",
        "navigation_source",
    }
)

SUMMARY_ROLES = frozenset({
    "primary_subject", "primary_article_body", "primary_article_text", "primary_media_player",
    "inline_article_media", "primary_cards", "secondary_recommendations", "sidebar_navigation",
    "advertisement", "breadcrumb", "detail_navigation_source",
})


def geometry_evidence(regions: SubjectRegions, key: str, geometry: Mapping) -> str:
    node = regions.nodes.get(key, {})
    raw = geometry.get(key, node.get("rect"))
    if raw is None:
        return "missing"
    rect = valid_rect(raw)
    if rect is None:
        return "insufficient"
    if not media_geometry_large_enough(rect):
        return "insufficient"
    for ancestor in regions.scopes.ancestors(key):
        if ancestor != regions.subject_root(key):
            continue
        container = valid_rect(geometry.get(ancestor, regions.nodes.get(ancestor, {}).get("rect")))
        if container is None:
            continue
        return "dominant" if media_dominates_subject(rect, container) else "insufficient"
    return "missing"


def summaries(regions: SubjectRegions, groups: dict[str, set[str]], evidence: dict[str, set[str]]):
    selectors, token_groups = [], []
    for role, keys in sorted(groups.items()):
        # 动态选择器完全不复制，结构标签来自节点白名单，身份只留不可逆指纹。
        safe = {_selector_fingerprint(regions.nodes.get(key, {}), key) for key in keys if key}
        selectors.append((role, tuple(sorted(safe)[:32])))
        semantic = set(evidence.get(role, set()))
        for key in keys:
            node = regions.nodes.get(key, {})
            semantic.update(tokens(node) & SEMANTIC_TOKENS)
            if node.get("tag") in SEMANTIC_TOKENS:
                semantic.add(str(node["tag"]))
        token_groups.append((role, tuple(sorted(semantic & SEMANTIC_TOKENS)[:32])))
    return tuple(selectors), tuple(token_groups)


def _selector_fingerprint(node: Mapping[str, object], key: str) -> str:
    tag = node.get("tag")
    shape = tag if tag in SEMANTIC_TOKENS else "node"
    return f"{shape}:sha256:{sha256(key.encode('utf-8')).hexdigest()[:24]}"
