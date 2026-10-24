"""组合区域语义与媒体证据，提供不依赖页型目录的结构事实。"""

from collections.abc import Mapping, Sequence

from services.api.app.application.web_subject_region_evidence import geometry_evidence, summaries
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.application.web_subject_region_scopes import (
    ARTICLE_TOKENS,
    GALLERY_TOKENS,
    VENDOR_TOKENS,
    SubjectRegions,
    safe_resource,
    tokens,
)


def extract_regions(
    snapshot: Mapping[str, object] | Sequence[Mapping[str, object]],
    *,
    element_geometry: Mapping[str, object] | None = None,
    navigation_sources: Sequence[Mapping[str, object]] = (),
) -> WebSubjectRegionFacts:
    raw = snapshot.get("elements", []) if isinstance(snapshot, Mapping) else snapshot
    raw = raw if isinstance(raw, (list, tuple)) else ()
    elements = [item for item in raw if isinstance(item, Mapping)]
    regions = SubjectRegions(elements)
    players, inline, galleries = set(), set(), set()
    player_evidence = set()
    for key in regions.primary:
        node = regions.nodes[key]
        if tokens(node) & GALLERY_TOKENS:
            members = [regions.nodes[item] for item in regions.members(key)]
            if any(
                item.get("tag") == "img" and safe_resource(item.get("src")) for item in members
            ) and any(
                item.get("tag") == "button"
                and tokens(item)
                & {"next", "previous", "prev", "zoom-in", "zoom-out", "viewer-control"}
                for item in members
            ):
                galleries.add(key)
        tag = node.get("tag")
        if tag not in {"video", "source", "iframe"}:
            continue
        owner = (
            str(node.get("parent_selector") or key)
            if tag == "source" and node.get("media_parent_tag") == "video"
            else key
        )
        if tag == "source" and node.get("media_parent_tag") != "video":
            continue
        independent_subject = regions.subject_root(owner)
        media_node = regions.nodes.get(owner, node)
        if (
            media_node.get("within_repeated_structure") is True
            or (
                type(media_node.get("repeat_group_size")) is int
                and media_node["repeat_group_size"] >= 2
            )
        ) and regions.nodes.get(independent_subject, {}).get("tag") != "article":
            # 重复预览不能作为独占播放器；同一原生播放器的多个 source 按宿主去重。
            continue
        media_state = node.get("media")
        controls_present = node.get("controls") is True or (
            isinstance(media_state, Mapping) and media_state.get("controls") is True
        )
        native_poster = tag == "video" and controls_present and safe_resource(node.get("poster"))
        if not safe_resource(node.get("src")) and not native_poster:
            continue
        if tag == "iframe":
            parent = str(node.get("parent_selector") or key)
            controls = [regions.nodes[item] for item in regions.members(parent)]
            has_play = any(
                item.get("tag") == "button"
                and str(item.get("aria_label") or item.get("text") or "").strip().casefold()
                in {"播放", "play", "播放视频", "play video"}
                for item in controls
            )
            has_timeline = any(
                item.get("tag") == "input" and item.get("type") == "range" for item in controls
            )
            if not (has_play and has_timeline):
                continue
        geometry = geometry_evidence(regions, owner, element_geometry or {})
        body_roots = {root for root in regions.bodies if regions.within(owner, {root})}
        sparse_article = bool(body_roots) and all(
            regions.nodes.get(root, {}).get("tag") in {"article", "main"}
            and not tokens(regions.nodes[root]) & ARTICLE_TOKENS
            and regions.body_lengths[root] < 300
            for root in body_roots
        )
        if body_roots and not (sparse_article and geometry == "dominant"):
            inline.add(owner)
        else:
            if geometry == "insufficient" or (
                regions.unscoped_prose and regions.subject_root(owner) == owner
            ):
                continue
            players.add(owner)
            player_evidence.add(
                "playback_controls" if tag == "iframe" or native_poster else "native_media_resource"
            )
            if geometry == "dominant":
                player_evidence.add("media_geometry_dominant")
            for ancestor in (owner, *regions.scopes.ancestors(owner)):
                player_evidence.update(tokens(regions.nodes.get(ancestor, {})) & VENDOR_TOKENS)
    subjects = {regions.subject_root(key) for key in players | galleries | regions.bodies}
    kind = "unknown"
    if len(subjects) > 1:
        kind = "conflicting"
    elif len(subjects) == 1:
        kind = "video" if players else "gallery" if galleries else "article"
    breadcrumbs = {
        key for key, node in regions.nodes.items() if tokens(node) & {"breadcrumb", "breadcrumbs"}
    }
    breadcrumbs = {
        key for key in breadcrumbs if not set(regions.scopes.ancestors(key)) & breadcrumbs
    }
    sources = {
        str(item["selector"])
        for item in navigation_sources
        if isinstance(item, Mapping)
        and item.get("candidate_role") == "detail"
        and isinstance(item.get("selector"), str)
        and item["selector"].strip()
    }
    groups = {
        "primary_subject": subjects,
        "primary_article_body": regions.bodies,
        "primary_article_text": {
            key for root in regions.bodies for key in regions.body_paragraphs[root]
        },
        "primary_media_player": players,
        "inline_article_media": inline,
        "primary_cards": regions.primary_cards,
        "secondary_recommendations": regions.recommendation_cards,
        "sidebar_navigation": regions.sidebar,
        "advertisement": regions.ads,
        "breadcrumb": breadcrumbs,
        "detail_navigation_source": sources,
    }
    selector_summaries, token_summaries = summaries(
        regions,
        groups,
        {
            "primary_media_player": player_evidence,
            "primary_article_body": {"paragraph_body"} if regions.bodies else set(),
            "primary_cards": {"repeated_cards"} if regions.primary_cards else set(),
            "detail_navigation_source": {"navigation_source"} if sources else set(),
        },
    )
    vendor_tokens = set()
    for key in players:
        for ancestor in (key, *regions.scopes.ancestors(key)):
            vendor_tokens.update(tokens(regions.nodes.get(ancestor, {})) & VENDOR_TOKENS)
    return WebSubjectRegionFacts(
        primary_subject_count=len(subjects),
        primary_subject_kind=kind,
        primary_article_body_count=len(regions.bodies),
        primary_article_text_length=sum(regions.body_lengths[root] for root in regions.bodies),
        primary_player_count=len(players),
        inline_article_media_count=len(inline),
        repeated_primary_card_count=len(regions.primary_cards),
        secondary_recommendation_card_count=len(regions.recommendation_cards),
        breadcrumb_count=len(breadcrumbs),
        detail_navigation_source_count=len(sources),
        player_vendor_tokens=tuple(sorted(vendor_tokens)),
        selector_summaries=selector_summaries,
        token_summaries=token_summaries,
    )
