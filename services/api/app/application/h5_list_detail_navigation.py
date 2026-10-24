"""H5 列表首卡到详情页的确定性能力合同。"""

from __future__ import annotations

import re
from collections.abc import Mapping

H5_LIST_DETAIL_TARGET = "当前列表首个内容卡片"
H5_POST_TITLE_SELECTOR = ".dynamicData-box:visible .title:visible"
H5_POST_CARD_SELECTOR = ".post-box.post:visible"
H5_COMMUNITY_POST_TITLE_SELECTOR = ".van-list .item_container .poi_title:visible"
H5_COMMUNITY_HOME_POST_TITLE_SELECTOR = (
    ".community_home .van-list .poi_title:visible"
)
H5_AI_PAGE_CARD_TITLE_SELECTOR = ".ai-page-card .ai-page-card-title:visible"
H5_COMIC_CARD_NAME_SELECTOR = ".comic-item .name:visible"
H5_MANGA_VIDEO_TITLE_SELECTOR = ".item-box-comics .duohang2:visible"
H5_CONTENT_LAYOUT3_COVER_SELECTOR = ".video-section .layout3 .img-box:visible"
H5_CONTENT_LAYOUT3_TITLE_SELECTOR = ".video-section .layout3 .name:visible"
H5_VIDEO_ITEM_SELECTOR = ".video-item:visible"
H5_LONG_VIDEO_CARD_SELECTOR = ".longVideoCard:visible"
H5_HORIZONTAL_VIDEO_CARD_SELECTOR = ".horizontalVideoBox.videoBox:visible"
H5_HOME_VIDEO_GRID_COVER_SELECTOR = (
    ".home-video-panel .TwoGridLayout .item .pic img:visible"
)
H5_HOME_VIDEO_GRID_TITLE_SELECTOR = (
    ".home-video-panel .TwoGridLayout .item .desc:visible"
)
H5_HOME_VIDEO_TITLE_SELECTOR = ".videoTitle:visible"
H5_MASONRY_VIDEO_TITLE_SELECTOR = ".masonry-grid .movie-item .video-title:visible"
H5_LONG_VIDEO_CARD_TITLE_SELECTOR = (
    ".longVideoCard .line-clamp-2 > p:visible"
)
H5_MODULE_VIDEO_TITLE_SELECTOR = ".modVideoBox .modCover_name:visible"
H5_DETAIL_ENTRY_SELECTOR = ".detail-entry:visible"
H5_POST_TEXT_TITLE_SELECTOR = ".post-box.post .post-text .text-overflow:visible"
H5_SQUARE_POST_TITLE_SELECTOR = (
    ".post-main-item .content-images-wrap .title:visible"
)
H5_BIG_VIDEO_TITLE_SELECTOR = "li.big-list .decial:visible"
H5_LIST_DETAIL_EXPECTED_PAGE_TYPE = {
    "home": "video_detail",
    "content_list": "content_detail",
    "post_list": "content_detail",
    "video_list": "video_detail",
}

_SELECTORS_BY_SOURCE = {
    "home": frozenset(
        {
            H5_HORIZONTAL_VIDEO_CARD_SELECTOR,
            H5_HOME_VIDEO_GRID_TITLE_SELECTOR,
            H5_HOME_VIDEO_TITLE_SELECTOR,
        }
    ),
    "content_list": frozenset(
        {
            H5_MODULE_VIDEO_TITLE_SELECTOR,
            H5_CONTENT_LAYOUT3_COVER_SELECTOR,
            H5_CONTENT_LAYOUT3_TITLE_SELECTOR,
            H5_POST_TITLE_SELECTOR,
            H5_COMMUNITY_HOME_POST_TITLE_SELECTOR,
            ".comics-item .title:visible",
            H5_COMIC_CARD_NAME_SELECTOR,
            H5_AI_PAGE_CARD_TITLE_SELECTOR,
            H5_DETAIL_ENTRY_SELECTOR,
            ".masonry-grid .data-item:visible",
            ".grid-item .data-item:visible",
            ".movie-list .media-item:visible",
            ".data-item:visible",
            ".media-item:visible",
            ".content-card:visible",
            ".ratio-box:visible",
        }
    ),
    "video_list": frozenset(
        {
            H5_BIG_VIDEO_TITLE_SELECTOR,
            H5_MANGA_VIDEO_TITLE_SELECTOR,
            H5_HOME_VIDEO_GRID_TITLE_SELECTOR,
            H5_MASONRY_VIDEO_TITLE_SELECTOR,
            H5_LONG_VIDEO_CARD_TITLE_SELECTOR,
            H5_LONG_VIDEO_CARD_SELECTOR,
            H5_HOME_VIDEO_GRID_COVER_SELECTOR,
            H5_HOME_VIDEO_GRID_COVER_SELECTOR,
            ".masonry-grid .movie-item .video-cover:visible",
            ".masonry-grid .grid-item .movie-item:visible",
            ".video-section .layout5 .img-box:visible",
            ".layout5 .name:visible",
            ".layout5 .img-box:visible",
            ".masonry-grid .movie-item:visible",
            ".grid-item .movie-item:visible",
            ".video-list .media-item:visible",
            ".movie-list .media-item:visible",
            ".masonry-grid .banner .banner-item:visible",
            ".masonry-grid .data-item:visible",
            ".movie-item:visible",
            H5_VIDEO_ITEM_SELECTOR,
            ".media-item:visible",
            ".data-item:visible",
        }
    ),
    "post_list": frozenset(
        {
            H5_POST_TEXT_TITLE_SELECTOR,
            H5_SQUARE_POST_TITLE_SELECTOR,
            H5_POST_TITLE_SELECTOR,
            H5_POST_CARD_SELECTOR,
            H5_COMMUNITY_POST_TITLE_SELECTOR,
            H5_COMMUNITY_HOME_POST_TITLE_SELECTOR,
            ".dynamic-box .title:visible",
            ".movie-list .media-item:visible .media-title:visible",
            ".movie-list .media-item:visible",
            ".post-list .post-item:visible",
            ".masonry-grid .data-item:visible",
            ".media-item:visible",
            ".post-item:visible",
            ".feed-item:visible",
        }
    ),
}
_DETAIL_ANCHOR_PATTERN = re.compile(r'^a\[href=(?P<quote>["\'])(?P<href>.+)(?P=quote)\]:visible$')
_SNAPSHOT_TOKENS_BY_SOURCE = {
    "home": frozenset({"horizontalvideobox", "videobox"}),
    "content_list": frozenset(
        {"ratio-img", "data-item", "media-item", "content-card"}
    ),
    "post_list": frozenset(
        {
            "dynamicdata-content",
            "post-box",
            "post",
            "media-title",
            "post-item",
            "feed-item",
            "data-item",
        }
    ),
    "video_list": frozenset(
        {
            "cover-img",
            "img-box",
            "longvideocard",
            "video-cover",
            "movie-item",
            "video-item",
            "media-item",
        }
    ),
}
_SNAPSHOT_TOKENS_BY_SELECTOR = {
    H5_BIG_VIDEO_TITLE_SELECTOR: frozenset({"decial"}),
    H5_AI_PAGE_CARD_TITLE_SELECTOR: frozenset({"ai-page-card-title"}),
    H5_CONTENT_LAYOUT3_COVER_SELECTOR: frozenset({"img-box"}),
    H5_CONTENT_LAYOUT3_TITLE_SELECTOR: frozenset({"name"}),
    # 长漫画列表可能先达到 Snapshot 元素/序列化上限，保留封面结构却截掉
    # 更靠下的 name 标题；comic-item 祖先门禁仍会排除横幅与版块标题。
    H5_COMIC_CARD_NAME_SELECTOR: frozenset(
        {"name", "aspect-ratio-content", "decode-image-main"}
    ),
    H5_HOME_VIDEO_GRID_COVER_SELECTOR: frozenset({"wh-full"}),
    H5_HOME_VIDEO_GRID_TITLE_SELECTOR: frozenset({"desc"}),
    H5_HOME_VIDEO_TITLE_SELECTOR: frozenset({"videotitle"}),
    H5_MANGA_VIDEO_TITLE_SELECTOR: frozenset({"duohang2"}),
    H5_MASONRY_VIDEO_TITLE_SELECTOR: frozenset({"video-title"}),
    H5_LONG_VIDEO_CARD_TITLE_SELECTOR: frozenset(
        {"longvideocard", "line-clamp-2"}
    ),
    H5_MODULE_VIDEO_TITLE_SELECTOR: frozenset({"modcover_name"}),
    H5_DETAIL_ENTRY_SELECTOR: frozenset({"detail-entry"}),
    H5_POST_TEXT_TITLE_SELECTOR: frozenset({"text-overflow"}),
    H5_SQUARE_POST_TITLE_SELECTOR: frozenset({"title"}),
    H5_LONG_VIDEO_CARD_SELECTOR: frozenset({"longvideocard"}),
    H5_POST_TITLE_SELECTOR: frozenset({"title"}),
    H5_POST_CARD_SELECTOR: frozenset({"post-box", "post"}),
    H5_COMMUNITY_POST_TITLE_SELECTOR: frozenset({"poi_title"}),
    H5_COMMUNITY_HOME_POST_TITLE_SELECTOR: frozenset({"poi_title"}),
    ".layout5 .name:visible": frozenset({"name"}),
    ".comics-item .title:visible": frozenset({"title"}),
    ".dynamic-box .title:visible": frozenset({"title"}),
}
_SNAPSHOT_ALL_ANCESTOR_TOKENS_BY_SELECTOR = {
    H5_BIG_VIDEO_TITLE_SELECTOR: frozenset({"big-list", "decial-wrap"}),
    H5_CONTENT_LAYOUT3_COVER_SELECTOR: frozenset({"layout3", "video-section"}),
    H5_CONTENT_LAYOUT3_TITLE_SELECTOR: frozenset({"layout3", "video-section"}),
    H5_HOME_VIDEO_GRID_COVER_SELECTOR: frozenset(
        {"pic", "item", "twogridlayout"}
    ),
    H5_HOME_VIDEO_GRID_TITLE_SELECTOR: frozenset(
        {"item", "twogridlayout", "home-video-panel__scroll"}
    ),
    H5_MASONRY_VIDEO_TITLE_SELECTOR: frozenset({"movie-item", "masonry-grid"}),
    H5_LONG_VIDEO_CARD_TITLE_SELECTOR: frozenset(
        {"longvideocard", "line-clamp-2"}
    ),
    H5_MODULE_VIDEO_TITLE_SELECTOR: frozenset({"modvideobox"}),
    H5_POST_TEXT_TITLE_SELECTOR: frozenset({"post-box", "post", "post-text"}),
    H5_SQUARE_POST_TITLE_SELECTOR: frozenset(
        {"post-main-item", "content-images-wrap"}
    ),
    H5_COMMUNITY_POST_TITLE_SELECTOR: frozenset({"item_container", "van-list"}),
    H5_COMMUNITY_HOME_POST_TITLE_SELECTOR: frozenset(
        {"van-list"}
    ),
}
_SNAPSHOT_OWN_OR_ANCESTOR_CHAIN_BY_SELECTOR = {
    H5_LONG_VIDEO_CARD_SELECTOR: (
        frozenset({"longvideocard"}),
        frozenset({"longvideocard", "item-wrapper", "row-four-card", "van-list"}),
    ),
}
_SNAPSHOT_TAG_BY_SELECTOR = {
    H5_HOME_VIDEO_GRID_COVER_SELECTOR: "img",
}
# 直接卡片 selector 的路由事件绑定在容器自身；必须由自身全部类名证明，
# 禁止正文叶节点仅凭祖先类名冒充可点击卡片。
_SNAPSHOT_OWN_TOKENS_BY_SELECTOR = {
    H5_HORIZONTAL_VIDEO_CARD_SELECTOR: frozenset(
        {"horizontalvideobox", "videobox"}
    ),
    H5_HOME_VIDEO_TITLE_SELECTOR: frozenset({"videotitle"}),
    H5_POST_CARD_SELECTOR: frozenset({"post-box", "post"}),
    H5_COMMUNITY_POST_TITLE_SELECTOR: frozenset({"poi_title"}),
    H5_COMMUNITY_HOME_POST_TITLE_SELECTOR: frozenset({"poi_title"}),
    H5_DETAIL_ENTRY_SELECTOR: frozenset({"detail-entry"}),
}
# 标题类记号（title/name）过于通用；当快照带祖先记号时要求标题落在
# 选择器声明的卡片容器内，避免命中版块头等无关标题。旧快照无该字段时
# 维持仅自身记号的兼容行为。
_SNAPSHOT_ANCESTOR_TOKENS_BY_SELECTOR = {
    H5_POST_TITLE_SELECTOR: frozenset({"dynamicdata-box", "dynamicdata-content"}),
    H5_COMMUNITY_HOME_POST_TITLE_SELECTOR: frozenset(
        {"community_home", "community_home_tab"}
    ),
    ".layout5 .name:visible": frozenset({"layout5"}),
    ".comics-item .title:visible": frozenset({"comics-item"}),
    H5_COMIC_CARD_NAME_SELECTOR: frozenset({"comic-item"}),
    H5_MANGA_VIDEO_TITLE_SELECTOR: frozenset({"item-box-comics"}),
    ".dynamic-box .title:visible": frozenset({"dynamic-box"}),
}


def h5_list_detail_selectors(source_page_type: str) -> tuple[str, ...]:
    """返回已登记的列表结构，供配对补证识别原始内容对象。"""
    return tuple(sorted(_SELECTORS_BY_SOURCE.get(source_page_type, ())))


def h5_list_detail_selector_allowed(*, source_page_type: str, selector: str) -> bool:
    """只允许检测器维护的结构 selector 或已验证的详情锚点。"""
    return selector in _SELECTORS_BY_SOURCE.get(source_page_type, ()) or (
        _DETAIL_ANCHOR_PATTERN.fullmatch(selector) is not None
    )


def h5_list_detail_expected_page_type(source_page_type: str) -> str | None:
    """返回列表页允许进入的规范详情类型。"""
    return H5_LIST_DETAIL_EXPECTED_PAGE_TYPE.get(source_page_type)


def h5_list_detail_selector_requires_visible_title(selector: str) -> bool:
    """标题型详情能力只接受当前视口内已证明无遮挡的标题。"""
    return selector in {
        H5_AI_PAGE_CARD_TITLE_SELECTOR,
        H5_COMIC_CARD_NAME_SELECTOR,
        H5_CONTENT_LAYOUT3_TITLE_SELECTOR,
        H5_COMMUNITY_HOME_POST_TITLE_SELECTOR,
        H5_COMMUNITY_POST_TITLE_SELECTOR,
        H5_HOME_VIDEO_GRID_TITLE_SELECTOR,
        H5_HOME_VIDEO_TITLE_SELECTOR,
        H5_MANGA_VIDEO_TITLE_SELECTOR,
        H5_MASONRY_VIDEO_TITLE_SELECTOR,
        H5_LONG_VIDEO_CARD_TITLE_SELECTOR,
        H5_MODULE_VIDEO_TITLE_SELECTOR,
        H5_POST_TEXT_TITLE_SELECTOR,
        H5_SQUARE_POST_TITLE_SELECTOR,
        H5_BIG_VIDEO_TITLE_SELECTOR,
        H5_POST_TITLE_SELECTOR,
        ".comics-item .title:visible",
        ".dynamic-box .title:visible",
        ".layout5 .name:visible",
        ".movie-list .media-item:visible .media-title:visible",
    }


def h5_list_detail_snapshot_element_matches(
    *,
    element: Mapping[str, object],
    source_page_type: str,
    selector: str,
) -> bool:
    """校验当前 Snapshot 元素能证明签发时的列表卡片结构仍存在。"""
    anchor_match = _DETAIL_ANCHOR_PATTERN.fullmatch(selector)
    if anchor_match is not None:
        return str(element.get("href") or "").strip() == anchor_match.group("href")
    required_tag = _SNAPSHOT_TAG_BY_SELECTOR.get(selector)
    if required_tag is not None and str(element.get("tag") or "").casefold() != required_tag:
        return False
    raw_own_tokens = element.get("class_tokens")
    own_tokens = (
        {
            str(item).strip().casefold()
            for item in raw_own_tokens
            if str(item).strip()
        }
        if isinstance(raw_own_tokens, list)
        else set()
    )
    raw_ancestors = element.get("ancestor_class_tokens")
    ancestor_tokens = (
        {
            str(item).strip().casefold()
            for item in raw_ancestors
            if str(item).strip()
        }
        if isinstance(raw_ancestors, list)
        else set()
    )
    own_or_ancestor_chain = _SNAPSHOT_OWN_OR_ANCESTOR_CHAIN_BY_SELECTOR.get(selector)
    if own_or_ancestor_chain is not None:
        required_own_tokens, required_ancestor_chain = own_or_ancestor_chain
        if not (
            required_own_tokens.issubset(own_tokens)
            or required_ancestor_chain.issubset(ancestor_tokens)
        ):
            return False
    required_own_tokens = _SNAPSHOT_OWN_TOKENS_BY_SELECTOR.get(selector)
    if required_own_tokens and not required_own_tokens.issubset(own_tokens):
        return False
    tokens: set[str] = set()
    # 卡片结构记号常挂在包装容器上（如 .img-box > img 的裸 img 自身无类名），
    # 因此祖先链记号与元素自身记号同等作为结构证明。
    for field in ("class_tokens", "ancestor_class_tokens"):
        raw_tokens = element.get(field)
        if isinstance(raw_tokens, list):
            tokens |= {str(item).strip().casefold() for item in raw_tokens if str(item).strip()}
    required_tokens = _SNAPSHOT_TOKENS_BY_SELECTOR.get(selector)
    if required_tokens is None:
        required_tokens = _SNAPSHOT_TOKENS_BY_SOURCE.get(source_page_type, frozenset())
    if not tokens & required_tokens:
        return False
    required_ancestors = _SNAPSHOT_ANCESTOR_TOKENS_BY_SELECTOR.get(selector)
    all_required_ancestors = _SNAPSHOT_ALL_ANCESTOR_TOKENS_BY_SELECTOR.get(selector)
    if all_required_ancestors:
        if not isinstance(raw_ancestors, list):
            return False
        normalized_ancestors = {
            str(item).strip().casefold()
            for item in raw_ancestors
            if str(item).strip()
        }
        if not all_required_ancestors.issubset(normalized_ancestors):
            return False
    if (
        required_ancestors
        and isinstance(raw_ancestors, list)
        and raw_ancestors
    ):
        ancestors = {str(item).strip().casefold() for item in raw_ancestors if str(item).strip()}
        return bool(ancestors & required_ancestors)
    return True
