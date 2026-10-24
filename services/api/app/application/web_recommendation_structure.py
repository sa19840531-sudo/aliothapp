"""推荐组件的通用结构边界；不依赖站点、URL 或正文内容。"""

import re
from collections.abc import Mapping

_COMPONENT = re.compile(r"(?:^|[-_])(?:recommend|recommendations|recommended|related)(?:[-_]|$)")


def is_web_recommendation_node(node: Mapping[str, object]) -> bool:
    """完整类名词段识别组件和祖先组件，避免普通子串误匹配。"""
    for field in ("class_tokens", "ancestor_class_tokens"):
        values = node.get(field)
        if isinstance(values, (list, tuple)) and any(
            isinstance(value, str) and _COMPONENT.search(value.casefold()) for value in values
        ):
            return True
    return False
