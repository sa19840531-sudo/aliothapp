"""将正文长度归属到已证明的主体，排除重复子树和次要区域。"""

from collections.abc import Mapping

from services.api.app.application.web_article_body_structure import is_web_article_body_node
from services.api.app.application.web_recommendation_structure import is_web_recommendation_node


def node_text_length(node: Mapping[str, object]) -> int:
    length = node.get("text_length")
    if type(length) is int and length >= 0:
        return length
    return len(str(node.get("text") or "").strip())


def collect_body_regions(regions):
    groups: dict[str, list[str]] = {}
    unscoped = set()
    for key in regions.primary:
        if regions.nodes[key].get("tag") != "p":
            continue
        root = regions.subject_root(key)
        if root == key or root not in regions.nodes:
            if node_text_length(regions.nodes[key]) >= 120:
                unscoped.add(key)
            continue
        groups.setdefault(root, []).append(key)
    lengths = {
        root: sum(node_text_length(regions.nodes[key]) for key in members)
        for root, members in groups.items()
    }
    # main 导语不能吞掉卡片；独立非重复正文子树仍保留，不能连真实正文一起排除。
    for root in tuple(groups):
        if not is_web_article_body_node(regions.nodes[root]) and any(
            regions.within(card, {root}) for card in regions.primary_cards
        ):
            body_members = []
            for key in groups[root]:
                parent = str(regions.nodes[key].get("parent_selector") or "")
                node = regions.nodes.get(parent, {})
                size = node.get("repeat_group_size")
                if parent != root and node.get("tag") in {"div", "section"} and not (
                    node.get("within_repeated_structure") is True
                    or (type(size) is int and size >= 2)
                    or any(regions.within(card, {parent}) for card in regions.primary_cards)
                ):
                    body_members.append(key)
            if body_members:
                groups[root] = body_members
                lengths[root] = sum(node_text_length(regions.nodes[key]) for key in body_members)
            else:
                groups.pop(root)
                lengths.pop(root)
    candidates = {
        key
        for key in regions.primary
        if regions.nodes[key].get("tag") == "main"
        or is_web_article_body_node(regions.nodes[key])
    }
    for root in candidates:
        if any(other != root and regions.within(other, {root}) for other in candidates):
            continue
        node = regions.nodes[root]
        if (
            node.get("tag") == "main"
            and not is_web_article_body_node(node)
            and root in regions.dominant_media_roots
            and lengths.get(root, 0) < 300
        ):
            # 独占播放器所在的通用 main 需有独立长段落，整页计数不能代替正文证据。
            continue
        trusted_length = regions.nodes[root].get("web_subject_text_length")
        if type(trusted_length) is int and 0 <= trusted_length <= 1000000:
            uncovered_secondary = any(
                regions.within(key, {root}) and not _counter_excludes(regions, key)
                for key in regions.secondary
            )
            article_root = is_web_article_body_node(node)
            nested_primary_cards = any(
                regions.within(card, {root}) for card in regions.primary_cards
            )
            # article 的正文计数已由浏览器排除次要区域；其表格、引用链接继承重复
            # 结构时仍属于正文。main 容器继续要求没有主卡片，避免列表被抬成详情。
            if not uncovered_secondary and (article_root or not nested_primary_cards):
                groups[root] = [root]
                lengths[root] = trusted_length
            continue
        if root in groups:
            continue
        excluded = (
            regions.secondary
            | regions.cards
            | {
                key
                for key, node in regions.nodes.items()
                if node.get("tag") in {"h1", "h2", "h3", "h4", "button", "input", "figcaption"}
            }
        )
        known_excluded = {
            key
            for key in regions.nodes
            if key != root
            and regions.within(key, {root})
            and regions.within(key, excluded)
            and node_text_length(regions.nodes[key]) > 0
        }
        top_excluded = {
            key for key in known_excluded if not set(regions.scopes.ancestors(key)) & known_excluded
        }
        secondary_roots = {
            key
            for key in regions.secondary | regions.cards
            if key != root and regions.within(key, {root})
        }
        if type(regions.nodes[root].get("text_length")) is int and any(
            type(regions.nodes[key].get("text_length")) is not int for key in secondary_roots
        ):
            # 完整主体总长不能减去截断的次要区域摘要，否则会制造正文。
            continue
        length = max(
            0,
            node_text_length(regions.nodes[root])
            - sum(node_text_length(regions.nodes[key]) for key in top_excluded),
        )
        if length >= 120:
            groups[root] = [root]
            lengths[root] = length
    return groups, lengths, unscoped


def _counter_excludes(regions, key: str) -> bool:
    for ancestor in (key, *regions.scopes.ancestors(key)):
        node = regions.nodes.get(ancestor, {})
        if ancestor in regions.ads or node.get("is_advertisement") is True:
            return True
        if is_web_recommendation_node(node):
            return True
        if node.get("tag") in {"nav", "aside", "footer"} or node.get("role") in {
            "navigation",
            "complementary",
            "button",
        }:
            return True
        classes = node.get("class_tokens")
        if isinstance(classes, (list, tuple)) and set(classes) & {
            "recommendations",
            "related",
            "related-content",
            "related-posts",
            "recommended",
            "advertisement",
            "advertising",
            "ad-slot",
            "sponsored",
        }:
            return True
    return False
