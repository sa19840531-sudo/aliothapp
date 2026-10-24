"""规范化 Web 稀疏 Snapshot 中的有限祖先摘要。"""

import re


def sanitize_web_ancestor_structure(
    value: object, *, selector_limit: int = 512
) -> list[dict[str, object]]:
    """只保留关系证明需要的三项事实；祖先数量、选择器与重复计数均有界。"""
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value[:32]:
        if not isinstance(item, dict):
            break
        selector = item.get("selector")
        tag = item.get("tag")
        size = item.get("repeat_group_size")
        if (
            not isinstance(selector, str)
            or not selector.strip()
            or len(selector) > selector_limit
            or selector in seen
            or not isinstance(tag, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", tag)
            or type(size) is not int
            or not 0 <= size <= 10000
        ):
            break
        seen.add(selector)
        result.append({"selector": selector.strip(), "tag": tag, "repeat_group_size": size})
    return result
