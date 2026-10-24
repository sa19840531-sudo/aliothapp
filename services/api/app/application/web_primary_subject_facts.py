"""从已排除导航和广告的节点计算主体媒体、活动与搜索组合事实。"""

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from services.api.app.application.web_article_body_structure import (
    ARTICLE_TOKENS,
    is_web_article_body_node,
)
from services.api.app.application.web_page_body_scopes import WebSnapshotBodyScopes
from services.api.app.application.web_subject_region_extraction import extract_regions
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts


def extract_subject_region_facts(
    snapshot: Mapping[str, object] | Sequence[Mapping[str, object]],
    *,
    element_geometry: Mapping[str, object] | None = None,
    navigation_sources: Sequence[Mapping[str, object]] = (),
) -> WebSubjectRegionFacts:
    """从脱敏快照提取区域事实。"""
    return extract_regions(
        snapshot, element_geometry=element_geometry, navigation_sources=navigation_sources
    )


def primary_subject_facts(
    elements: Sequence[Mapping[str, object]],
    *,
    proven_body_scopes: set[str] | None = None,
) -> dict[str, int]:
    """只组合可信 DOM 属性，不接受模型自报的媒体或页型标记。"""

    scopes = WebSnapshotBodyScopes(list(elements))

    def tokens(item, field="class_tokens"):
        value = item.get(field)
        return {str(token).casefold() for token in value} if isinstance(value, list) else set()

    def tag(item):
        return str(item.get("tag") or "").casefold()

    def selector(item):
        return str(item.get("selector") or "")

    def repeated(item):
        size = item.get("repeat_group_size")
        return (
            item.get("within_repeated_structure") is True
            or item.get("is_repeated_card") is True
            or (type(size) is int and size >= 2)
        )

    def within(item, root):
        return bool(selector(root)) and scopes.within(selector(item), {selector(root)})

    article_tokens = ARTICLE_TOKENS
    gallery_tokens = {
        "image-gallery",
        "photo-gallery",
        "image-viewer",
        "photo-viewer",
        "gallery",
        "lightbox",
    }
    player_tokens = {
        "video-player",
        "video-js",
        "player-root",
        "main-player",
        "playback",
        "detail__play",
        "hg-web-detail__play",
    }
    repeated_roots = [item for item in elements if repeated(item)]
    primary = [
        item
        for item in elements
        if not repeated(item) and not any(within(item, root) for root in repeated_roots)
    ]
    articles = [item for item in primary if is_web_article_body_node(item)]
    galleries = [item for item in primary if tokens(item) & gallery_tokens]
    gallery_count = control_count = 0
    for root in galleries:
        members = [item for item in elements if within(item, root)]
        controls = [
            item
            for item in members
            if tag(item) in {"button", "a", "input"} or item.get("role") == "button"
        ]
        controls = [
            item
            for item in controls
            if tokens(item)
            & {
                "next",
                "previous",
                "prev",
                "zoom-in",
                "zoom-out",
                "gallery-next",
                "gallery-prev",
                "viewer-control",
            }
        ]
        if (
            any(tag(item) == "img" and str(item.get("src") or "").strip() for item in members)
            and controls
        ):
            gallery_count += 1
            control_count += len(controls)

    confirmed = []
    for item in primary:
        # source 与所属 video 使用同一主体位置，不能把资源子节点误当独立播放器分支。
        media_owner = (
            str(item.get("parent_selector") or "")
            if tag(item) == "source" and item.get("media_parent_tag") == "video"
            else selector(item)
        )
        media_ancestors = scopes.ancestors(media_owner)
        # 布局容器的段落不能吞并兄弟媒体子树；独立性按父链证明，不依赖容器标签或类名。
        within_proven_body = any(
            scopes.within(selector(item), {body_root})
            and not (media_ancestors and media_ancestors[0] != body_root)
            for body_root in proven_body_scopes or set()
        )
        if within_proven_body:
            continue
        is_video = tag(item) == "video" or (
            tag(item) == "source" and item.get("media_parent_tag") == "video"
        )
        src = item.get("src")
        if not is_video or not isinstance(src, str) or not src.strip():
            continue
        try:
            scheme = urlsplit(src.strip()).scheme.casefold()
        except ValueError:
            continue
        if scheme not in {"", "http", "https", "blob", "data"}:
            continue
        if (tokens(item) | tokens(item, "ancestor_class_tokens")) & {
            "background-video",
            "video-background",
            "video-preview",
            "preview-video",
        }:
            continue
        if any(within(item, root) for root in articles + galleries) or tokens(
            item, "ancestor_class_tokens"
        ) & (article_tokens | gallery_tokens):
            continue
        confirmed.append(item)
    shells = [item for item in primary if tag(item) == "video" or tokens(item) & player_tokens]
    shell_count = sum(
        not any(item is resource or within(resource, item) for resource in confirmed)
        for item in shells
    )

    activity_roots = [
        item
        for item in primary
        if tokens(item) & {"activity", "campaign", "activity-landing", "campaign-landing"}
    ]
    activity_count = action_count = 0
    for root in activity_roots:
        actions = [
            item
            for item in elements
            if within(item, root)
            and (
                (
                    (tag(item) == "button" or item.get("role") == "button")
                    and tokens(item)
                    & {
                        "participate",
                        "join",
                        "claim",
                        "redeem",
                        "register",
                        "activity-action",
                        "campaign-action",
                        "cta",
                    }
                )
                or (
                    tag(item) == "a"
                    and item.get("href")
                    and not tokens(item) & {"close", "dismiss"}
                )
            )
        ]
        if actions:
            activity_count += 1
            action_count += len(actions)

    # 全局页头中的搜索入口不是页面搜索主体；必须读取父链，不能看按钮文案。
    def primary_search_region(item):
        chain = (selector(item), *scopes.ancestors(selector(item)))
        for index, key in enumerate(chain):
            node = scopes.nodes.get(key, {})
            if tag(node) in {"nav", "aside", "footer"} or node.get("role") in {
                "navigation", "complementary", "banner",
            }:
                return False
            if tag(node) == "header" and not any(
                tag(scopes.nodes.get(parent, {})) in {"main", "article", "section"}
                for parent in chain[index + 1:]
            ):
                return False
        return True

    search_primary = [item for item in primary if primary_search_region(item)]
    explicit_search_roots = [item for item in search_primary if item.get("role") == "search"]
    search_inputs = [
        item
        for item in search_primary
        if tag(item) == "input"
        and (
            item.get("type") == "search"
            or item.get("role") == "searchbox"
            or tokens(item) & {"search-input", "query-input"}
            or (
                item.get("type", "text") == "text"
                and any(within(item, root) for root in explicit_search_roots)
            )
        )
    ]
    search_roots = [
        item
        for item in search_primary
        if item.get("role") == "search"
        or (tag(item) == "form" and any(within(inp, item) for inp in search_inputs))
    ]
    search_triggers = [
        item
        for item in search_primary
        if (tag(item) in {"button", "input"} and item.get("type") == "submit")
        or (
            (tag(item) == "button" or item.get("role") == "button")
            and tokens(item) & {"search-submit", "search-trigger", "search-button"}
        )
    ]
    paired_triggers = [
        item
        for item in search_triggers
        if any(
            within(item, root) and any(within(inp, root) for inp in search_inputs)
            for root in search_roots
        )
    ]
    result_roots = [
        item for item in search_primary if tokens(item) & {"search-results", "query-results"}
    ]
    result_count = sum(
        any(repeated(item) and within(item, root) for item in elements) for root in result_roots
    )
    return {
        "image_gallery_root_count": gallery_count,
        "image_viewer_control_count": control_count,
        "confirmed_primary_video_resource_count": len(confirmed),
        "player_shell_without_resource_count": shell_count,
        "opaque_primary_media_count": sum(
            tag(item) in {"canvas", "iframe", "object", "embed"} for item in primary
        ),
        "search_input_count": len(search_inputs),
        "search_trigger_count": len(paired_triggers),
        "query_result_group_count": result_count,
        "activity_primary_region_count": activity_count,
        "activity_action_count": action_count,
    }
