"""Web 正文根语义：完整标签、类名与 ID，不能由 URL 或标题推断。"""

import re
from collections.abc import Mapping

ARTICLE_TOKENS = frozenset({
    "article-body", "post-content", "entry-content", "content-detail", "detail-content",
    "markdown-body",
})
WEB_ARTICLE_BODY_SELECTOR = ",".join([
    "article", "[role='article']",
    *(f".{token}" for token in sorted(ARTICLE_TOKENS)),
    *(f"#{token}" for token in sorted(ARTICLE_TOKENS)),
])
# 通用 Markdown 编辑器的只读预览根，只在 Web 采集入口追加，不改变共享默认选择器。
WEB_RENDERED_ARTICLE_TOKENS = frozenset({"editormd-preview", "markdown-preview"})
WEB_RENDERED_ARTICLE_SELECTOR = ",".join(
    f".{token}" for token in sorted(WEB_RENDERED_ARTICLE_TOKENS)
)


def is_web_article_body_node(node: Mapping[str, object]) -> bool:
    if node.get("tag") == "article" or node.get("role") == "article":
        return True
    if node.get("tag") not in {"main", "div", "section"}:
        return False
    classes = node.get("class_tokens")
    if isinstance(classes, (list, tuple)) and (
        ARTICLE_TOKENS | WEB_RENDERED_ARTICLE_TOKENS
    ).intersection(
        value.casefold() for value in classes if isinstance(value, str)
    ):
        return True
    # 只识别当前节点的完整 ID，不消费祖先路径或属性值中的正文词。
    leaf = re.split(r"\s+|[>+~]", str(node.get("selector") or ""))[-1]
    match = re.fullmatch(r"(?:[a-z][\w-]*)?#([a-z][\w-]*)", leaf, re.IGNORECASE)
    return bool(match and match.group(1).casefold() in ARTICLE_TOKENS)
