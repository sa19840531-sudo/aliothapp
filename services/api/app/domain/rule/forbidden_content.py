"""禁止内容库扫描：多分类穷举匹配，供敏感词 Tool 调用。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

REQUIRED_FORBIDDEN_CONTENT_CATEGORIES = (
    "political",
    "terrorism",
    "personal_attack",
    "china_related",
    "adult",
    "gambling",
    "private_ad",
    "phishing",
    "uk_politics",
    "minor_redline",
)

_ASCII_ALNUM = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)

_CATALOG_PATH = Path(__file__).with_name("forbidden_content_catalog.json")


@dataclass(frozen=True)
class ForbiddenContentCategory:
    """禁止内容分类及其词条。"""

    category: str
    label: str
    terms: tuple[str, ...]
    whitelist_terms: tuple[str, ...]


@dataclass(frozen=True)
class ForbiddenContentCatalog:
    """禁止内容库快照。"""

    version: str
    categories: tuple[ForbiddenContentCategory, ...]


@dataclass(frozen=True)
class SensitiveWordHit:
    """一次敏感词命中。"""

    term: str
    category: str
    category_label: str
    start: int
    end: int
    match_type: str
    is_whitelisted: bool

    def to_dict(self) -> dict[str, str | int | bool]:
        """转换为 Tool 可返回的结构化字典。"""
        return {
            "term": self.term,
            "category": self.category,
            "category_label": self.category_label,
            "start": self.start,
            "end": self.end,
            "match_type": self.match_type,
            "is_whitelisted": self.is_whitelisted,
        }


def scan_sensitive_words(
    text: str,
    catalog: ForbiddenContentCatalog | None = None,
    categories: Sequence[str] | None = None,
) -> dict[str, Any]:
    """按禁止内容库穷举扫描文本，返回可审计命中结果。

    categories 为分类作用域：传入时只扫描指定分类（未知分类抛 ValueError）；
    缺省扫描全库，行为与历史版本一致。
    """
    source_text = text or ""
    active_catalog = catalog if catalog is not None else load_default_forbidden_content_catalog()
    scan_categories = _resolve_scan_categories(active_catalog, categories)
    hits = _scan_hits(source_text, scan_categories)
    return {
        "execution_status": "success",
        "catalog_version": active_catalog.version,
        "scanned_categories": [item.category for item in scan_categories],
        "text_length": len(source_text),
        "hit_count": len(hits),
        "effective_hit_count": sum(1 for hit in hits if not hit.is_whitelisted),
        "hits": [hit.to_dict() for hit in hits],
    }


def _resolve_scan_categories(
    catalog: ForbiddenContentCatalog,
    categories: Sequence[str] | None,
) -> tuple[ForbiddenContentCategory, ...]:
    if categories is None:
        return catalog.categories
    requested = tuple(dict.fromkeys(str(item) for item in categories))
    known = {item.category for item in catalog.categories}
    unknown = tuple(item for item in requested if item not in known)
    if unknown:
        raise ValueError(f"敏感词分类作用域包含词库不存在的分类：{unknown}")
    selected = set(requested)
    return tuple(item for item in catalog.categories if item.category in selected)


@lru_cache
def load_default_forbidden_content_catalog() -> ForbiddenContentCatalog:
    """加载默认禁止内容库。"""
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    categories = tuple(_parse_category(item) for item in raw["categories"])
    _validate_categories(categories)
    return ForbiddenContentCatalog(version=str(raw["version"]), categories=categories)


def _parse_category(item: dict[str, Any]) -> ForbiddenContentCategory:
    return ForbiddenContentCategory(
        category=str(item["category"]),
        label=str(item["label"]),
        terms=tuple(str(term) for term in item.get("terms", [])),
        whitelist_terms=tuple(str(term) for term in item.get("whitelist_terms", [])),
    )


def _validate_categories(categories: tuple[ForbiddenContentCategory, ...]) -> None:
    actual = tuple(item.category for item in categories)
    if actual != REQUIRED_FORBIDDEN_CONTENT_CATEGORIES:
        raise ValueError(
            "禁止内容库分类必须按固定顺序对齐："
            f"{REQUIRED_FORBIDDEN_CONTENT_CATEGORIES}，当前为：{actual}"
        )


def _scan_hits(
    text: str,
    categories: tuple[ForbiddenContentCategory, ...],
) -> list[SensitiveWordHit]:
    hits: list[SensitiveWordHit] = []
    for category in categories:
        for term in category.terms:
            hits.extend(_scan_term(text=text, term=term, category=category))
    return sorted(hits, key=lambda item: (item.start, item.end, item.category, item.term))


def _scan_term(
    *,
    text: str,
    term: str,
    category: ForbiddenContentCategory,
) -> list[SensitiveWordHit]:
    if not term:
        return []
    if _is_ascii_word_term(term):
        return _scan_ascii_word_term(text=text, term=term, category=category)

    hits: list[SensitiveWordHit] = []
    start = text.find(term)
    while start >= 0:
        end = start + len(term)
        hits.append(
            SensitiveWordHit(
                term=term,
                category=category.category,
                category_label=category.label,
                start=start,
                end=end,
                match_type="contains",
                is_whitelisted=_is_whitelisted(text=text, start=start, end=end, category=category),
            )
        )
        start = text.find(term, start + 1)
    return hits


def _is_ascii_word_term(term: str) -> bool:
    """纯 ASCII 且含字母的词条（英文词/缩写）走整词匹配，避免子串误伤。"""
    return term.isascii() and any(ch.isalpha() for ch in term)


def _scan_ascii_word_term(
    *,
    text: str,
    term: str,
    category: ForbiddenContentCategory,
) -> list[SensitiveWordHit]:
    # 仅折叠 ASCII 字母，保证折叠前后长度与索引一一对应（casefold 对 ß/İ 等会变长）。
    folded_text = "".join(ch.lower() if ch.isascii() else ch for ch in text)
    folded_term = term.lower()
    hits: list[SensitiveWordHit] = []
    start = folded_text.find(folded_term)
    while start >= 0:
        end = start + len(folded_term)
        boundary_ok = (start == 0 or folded_text[start - 1] not in _ASCII_ALNUM) and (
            end == len(folded_text) or folded_text[end] not in _ASCII_ALNUM
        )
        if boundary_ok:
            hits.append(
                SensitiveWordHit(
                    term=term,
                    category=category.category,
                    category_label=category.label,
                    start=start,
                    end=end,
                    match_type="word",
                    is_whitelisted=_is_whitelisted(
                        text=text, start=start, end=end, category=category
                    ),
                )
            )
        start = folded_text.find(folded_term, start + 1)
    return hits


def _is_whitelisted(
    *,
    text: str,
    start: int,
    end: int,
    category: ForbiddenContentCategory,
) -> bool:
    for whitelist_term in category.whitelist_terms:
        whitelist_start = text.find(whitelist_term)
        while whitelist_start >= 0:
            whitelist_end = whitelist_start + len(whitelist_term)
            if whitelist_start <= start and end <= whitelist_end:
                return True
            whitelist_start = text.find(whitelist_term, whitelist_start + 1)
    return False
