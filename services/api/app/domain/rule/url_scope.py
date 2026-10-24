"""规则 URL 适用范围匹配（候选规则过滤）。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# 探索 Run 已收终态后，仍允许基于不可变证据补建 Publication。
TERMINAL_AGENT_RUN_STATUSES: frozenset[str] = frozenset(
    {"done", "failed", "aborted", "canceled"}
)
ACTIVE_AGENT_RUN_STATUSES: frozenset[str] = frozenset({"pending", "running"})
FROZEN_PUBLICATION_SOURCE_RUN_STATUSES: frozenset[str] = (
    ACTIVE_AGENT_RUN_STATUSES | TERMINAL_AGENT_RUN_STATUSES
)

_OVERLY_BROAD_URL_PATTERNS: frozenset[str] = frozenset(
    {".*", ".+", "^.*$", ".*$", "^.*"}
)


class InvalidRuleUrlPatternError(ValueError):
    """规则 url_pattern 非法或过于宽泛。"""


def normalize_rule_scope_url(value: str | None) -> str | None:
    """规范化页面 URL，与浏览器采集侧口径对齐。"""
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if not parsed.scheme:
        return text
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def compile_rule_url_pattern(pattern: str) -> re.Pattern[str]:
    """编译规则 url_pattern；非法或全匹配模式 fail-closed。"""
    text = str(pattern or "").strip()
    if not text:
        raise InvalidRuleUrlPatternError("url_pattern 不能为空")
    if text in _OVERLY_BROAD_URL_PATTERNS:
        raise InvalidRuleUrlPatternError("url_pattern 过于宽泛")
    try:
        compiled = re.compile(text)
    except re.error as exc:
        raise InvalidRuleUrlPatternError(f"url_pattern 不是有效正则：{exc}") from exc
    if compiled.pattern in _OVERLY_BROAD_URL_PATTERNS:
        raise InvalidRuleUrlPatternError("url_pattern 过于宽泛")
    return compiled


def page_url_matches_pattern(page_url: str, pattern: str) -> bool:
    """判断规范化 URL 是否命中规则 pattern。"""
    compiled = compile_rule_url_pattern(pattern)
    return compiled.search(page_url) is not None


def candidate_rule_matches_page_url(
    *,
    page_url: str | None,
    url_pattern: str | None,
) -> tuple[bool, str | None]:
    """候选规则是否适用于当前页面 URL。

    返回 (是否纳入候选, match_reason 覆盖值)。
  无 pattern 时视为全 URL 适用；有 pattern 但缺少 page_url 时 fail-closed。
    """
    pattern_text = str(url_pattern or "").strip()
    if not pattern_text:
        return True, None
    normalized_url = normalize_rule_scope_url(page_url)
    if normalized_url is None:
        return False, None
    try:
        if not page_url_matches_pattern(normalized_url, pattern_text):
            return False, None
    except InvalidRuleUrlPatternError:
        return False, None
    return True, "url_pattern"
