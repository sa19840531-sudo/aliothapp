"""以可信父链建立主体、正文与次要区域边界。"""

import math
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from services.api.app.application.web_article_body_structure import (
    ARTICLE_TOKENS as ARTICLE_TOKENS,
)
from services.api.app.application.web_article_body_structure import (
    is_web_article_body_node,
)
from services.api.app.application.web_detail_candidate_extraction import (
    web_card_anchor_is_auxiliary,
)
from services.api.app.application.web_page_body_scopes import WebSnapshotBodyScopes
from services.api.app.application.web_recommendation_structure import is_web_recommendation_node
from services.api.app.application.web_subject_body_regions import collect_body_regions

GALLERY_TOKENS = {
    "image-gallery",
    "photo-gallery",
    "image-viewer",
    "photo-viewer",
    "gallery",
    "lightbox",
}
RECOMMENDATION_TOKENS = {
    "recommendations",
    "related",
    "related-content",
    "related-posts",
    "recommended",
}
VENDOR_TOKENS = {"dplayer", "video-js", "plyr", "jwplayer"}


def tokens(node: Mapping[str, object], field: str = "class_tokens") -> set[str]:
    values = node.get(field)
    return (
        {value.casefold() for value in values if isinstance(value, str)}
        if isinstance(values, (list, tuple))
        else set()
    )


def safe_resource(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return urlsplit(value.strip()).scheme.casefold() in {"", "http", "https", "blob", "data"}
    except ValueError:
        return False


def valid_rect(raw: object) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    values = tuple(raw.get(field, 0) for field in ("x", "y", "width", "height"))
    if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        return None
    return values if values[2] > 0 and values[3] > 0 else None


def media_geometry_large_enough(media_rect: tuple) -> bool:
    _, _, width, height = media_rect
    return width >= 240 and height >= 120 and width * height >= 28800


def media_dominates_subject(media_rect: tuple, subject_rect: tuple) -> bool:
    x, y, width, height = media_rect
    rx, ry, rw, rh = subject_rect
    if not media_geometry_large_enough(media_rect):
        return False
    # 长滚动主体按根宽界定一屏面积，避免下方内容稀释主播放器。
    ratio = width * height / (rw * min(rh, rw))
    overlap_width = max(0, min(x + width, rx + rw) - max(x, rx))
    overlap_height = max(0, min(y + height, ry + rh) - max(y, ry))
    coverage = overlap_width * overlap_height / (width * height)
    return ratio >= 0.12 and coverage >= 0.8


class SubjectRegions:
    def __init__(self, elements: Sequence[Mapping[str, object]]):
        self.scopes = WebSnapshotBodyScopes(elements)
        self.nodes = self.scopes.nodes
        self.recommendations = {
            key
            for key, node in self.nodes.items()
            if is_web_recommendation_node(node)
            or node.get("web_secondary_recommendation") is True
        }
        for node in self.nodes.values():
            if node.get("tag") in {"h2", "h3"} and str(
                node.get("text") or ""
            ).strip().casefold() in {
                "相关推荐",
                "猜你喜欢",
                "更多内容",
                "related content",
                "recommended",
                "you may also like",
            }:
                parent = str(node.get("parent_selector") or "")
                if self.nodes.get(parent, {}).get("tag") in {"section", "aside", "div"}:
                    self.recommendations.add(parent)
        self.sidebar = {
            key
            for key, node in self.nodes.items()
            if node.get("tag") in {"aside", "nav", "footer"}
            or (
                # 站点页头不是正文；文章、主区域或分节内部的 header 仍属于所属主体。
                node.get("tag") == "header"
                and not any(
                    self.nodes.get(parent, {}).get("tag") in {"article", "main", "section"}
                    or self.nodes.get(parent, {}).get("role") in {"article", "main"}
                    for parent in self.scopes.ancestors(key)
                )
            )
            or node.get("role") in {"navigation", "complementary"}
            or (
                node.get("within_web_navigation") is True
                if isinstance(node.get("within_web_navigation"), bool)
                else node.get("within_navigation") is True
            )
            or any(
                node.get(flag) is True
                for flag in (
                    "within_footer",
                    "within_pagination",
                )
            )
        } - self.recommendations
        self.ads = {
            key
            for key, node in self.nodes.items()
            if node.get("is_advertisement") is True
            or (tokens(node) | tokens(node, "ancestor_class_tokens"))
            & {"advertisement", "advertising", "ad-slot", "sponsored"}
        }
        self.excluded_media = {
            key
            for key, node in self.nodes.items()
            if (tokens(node) | tokens(node, "ancestor_class_tokens"))
            & {"video-preview", "preview-video", "background-video", "video-background"}
        }
        self.secondary = self.recommendations | self.sidebar | self.ads | self.excluded_media
        self.dominant_media_roots = set()
        for key, node in self.nodes.items():
            if node.get("tag") != "video" or not safe_resource(node.get("src")):
                continue
            state = node.get("media")
            size = node.get("repeat_group_size")
            if (
                not isinstance(state, Mapping)
                or type(state.get("ready_state")) is not int
                or state["ready_state"] < 2
                or node.get("within_repeated_structure") is True
                or (type(size) is int and size >= 2)
                or self.within(key, self.secondary)
            ):
                continue
            root = self.subject_root(key)
            media_rect = valid_rect(node.get("rect"))
            root_rect = valid_rect(self.nodes.get(root, {}).get("rect"))
            if media_rect and root_rect and media_dominates_subject(media_rect, root_rect):
                self.dominant_media_roots.add(root)
        anchors = {
            key
            for key, node in self.nodes.items()
            if node.get("tag") == "a" and safe_resource(node.get("href"))
            and not web_card_anchor_is_auxiliary(node)
        }
        cards = set()
        for key, node in self.nodes.items():
            size = node.get("repeat_group_size")
            repeated = (
                node.get("is_repeated_card") is True
                or node.get("within_repeated_structure") is True
                or (type(size) is int and size >= 2)
            )
            if (
                repeated
                and node.get("tag") in {"li", "article", "div", "section", "a"}
                and len(
                    {self.nodes[anchor]["href"] for anchor in anchors if self.within(anchor, {key})}
                )
                == 1
            ):
                cards.add(key)
        # 重复操作和元信息链接不是列表卡片；真正的卡片需有独立内容或可见同级结构。
        qualified = {
            key for key in cards
            if self.within(key, self.secondary) or self._primary_card_qualified(key, cards)
        }
        # 同一卡片的外壳、链接与媒体只计一次，段落重复不能成为卡片。
        self.cards = {
            key for key in qualified if not set(self.scopes.ancestors(key)) & qualified
        }
        self.primary_cards = {key for key in self.cards if not self.within(key, self.secondary)}
        self.recommendation_cards = {
            key
            for key in self.cards
            if self.within(key, self.recommendations)
            and not self.within(key, self.sidebar | self.ads)
        }
        self.primary = {
            key for key in self.nodes if not self.within(key, self.secondary | self.cards)
        }
        self.body_paragraphs, self.body_lengths, self.unscoped_prose = collect_body_regions(self)
        self.bodies = {root for root, length in self.body_lengths.items() if length >= 120}
        # 已证明的文章主体吸收内部重复链接；它们不能再作为页面级列表卡片冲突。
        self.primary_cards = {
            key for key in self.primary_cards if not self.within(key, self.bodies)
        }
        if len(self.bodies) == 1:
            body = next(iter(self.bodies))
            article_roots = {
                parent for parent in (body, *self.scopes.ancestors(body))
                if self.nodes.get(parent, {}).get("tag") == "article"
                or self.nodes.get(parent, {}).get("role") == "article"
            }
            if len(article_roots) == 1:
                article_root = next(iter(article_roots))
                article_context = {
                    parent for parent in self.scopes.ancestors(article_root)
                    if self.nodes.get(parent, {}).get("tag") in {"section", "main"}
                }
                # 单篇正文的标签与前后篇属于操作入口，只有独立标题或封面才可能是卡。
                self.primary_cards = {
                    key for key in self.primary_cards
                    if not self._article_auxiliary_card(
                        key, article_root=article_root, article_context=article_context
                    )
                }

    def within(self, key: str, roots: set[str]) -> bool:
        return self.scopes.within(key, roots)

    def members(self, root: str) -> list[str]:
        return [key for key in self.primary if self.within(key, {root})]

    def _primary_card_qualified(self, key: str, cards: set[str]) -> bool:
        if self._has_independent_card_content(key):
            return True
        if self.subject_root(key) in self.dominant_media_roots:
            return False
        node = self.nodes[key]
        parent = str(node.get("parent_selector") or "")
        peers = sum(
            str(self.nodes[other].get("parent_selector") or "") == parent
            and self.nodes[other].get("tag") == node.get("tag")
            for other in cards
        )
        return peers >= 2

    def _has_independent_card_content(self, key: str) -> bool:
        for child_key, child in self.nodes.items():
            if child_key == key or not self.within(child_key, {key}):
                continue
            if child.get("tag") in {"h2", "h3"} and str(child.get("text") or "").strip():
                return True
            if child.get("tag") == "img" and safe_resource(child.get("src")):
                rect = valid_rect(child.get("rect"))
                if rect and rect[2] >= 120 and rect[3] >= 80:
                    return True
        return False

    def _article_auxiliary_card(
        self, key: str, *, article_root: str, article_context: set[str]
    ) -> bool:
        if self._has_independent_card_content(key):
            return False
        node = self.nodes[key]
        classes = (
            *tokens(node),
            *tokens(node, "ancestor_class_tokens"),
        )
        parts = [set(re.split(r"[-_]+", value)) for value in classes]
        if self.within(key, {article_root}) and any(
            value & {"tag", "tags", "category", "categories", "taxonomy", "filter", "search"}
            for value in parts
        ):
            return True
        return self.within(key, article_context) and any(
            value & {"nav", "prev", "previous", "next"}
            and value & {"article", "post", "entry", "detail"}
            for value in parts
        )

    def subject_root(self, key: str) -> str:
        chain = (key, *self.scopes.ancestors(key))
        for parent in chain:
            node = self.nodes.get(parent, {})
            if is_web_article_body_node(node):
                return parent
        for parent in chain:
            if self.nodes.get(parent, {}).get("tag") == "main":
                return parent
        containers = [
            parent
            for parent in chain
            if self.nodes.get(parent, {}).get("tag") in {"body", "html", "div", "section"}
        ]
        return containers[-1] if containers else key
