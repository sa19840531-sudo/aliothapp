"""页面导航别名解析。"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.page.registry import get_default_page_type_registry

logger = logging.getLogger(__name__)

PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS = 1536
PAGE_NAV_ALIAS_EMBEDDING_MODEL = "alioth-local-nav-alias-v1"
DEFAULT_VECTOR_MIN_SIMILARITY = 0.8


@dataclass(frozen=True)
class PageNavAliasTerm:
    canonical_code: str
    page_subtype_code: str | None
    terms: tuple[str, ...]
    source_category: str
    confidence: float = 0.95


@dataclass(frozen=True)
class PageNavAliasMatch:
    canonical_code: str
    page_subtype_code: str | None
    matched_term: str
    source_category: str
    confidence: float
    match_type: str = "static"


@dataclass(frozen=True)
class PageNavAliasResolution:
    """单次导航知识解析的安全审计事实。"""

    match: PageNavAliasMatch | None
    source: str
    repository_configured: bool
    exact_lookup_attempted: bool = False
    vector_lookup_attempted: bool = False
    repository_degraded: bool = False
    degraded_stage: str | None = None
    degraded_code: str | None = None


@dataclass(frozen=True)
class PageNavAliasSeedRow:
    canonical_code: str
    page_subtype_code: str | None
    label: str
    description: str
    normalized_text: str
    embedding_text: str
    source_category: str
    confidence: float
    embedding: tuple[float, ...]


class PageNavAliasRepositoryProtocol(Protocol):
    async def find_confirmed_exact(
        self,
        normalized_text: str,
    ) -> PageNavAliasMatch | None:
        """按归一化文案读取已确认、启用的别名。"""

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        """按本地 embedding 召回已确认、启用的别名。"""


class PageNavAliasResolver:
    """统一页面导航别名解析服务，DB 不可用时回退到静态别名。"""

    def __init__(
        self,
        *,
        repository: PageNavAliasRepositoryProtocol | None = None,
        vector_min_similarity: float = DEFAULT_VECTOR_MIN_SIMILARITY,
    ) -> None:
        self._repository = repository
        self._repository_disabled_until = 0.0
        self._vector_min_similarity = vector_min_similarity

    async def resolve(self, label: str) -> PageNavAliasMatch | None:
        """兼容既有调用方，只返回正式 Catalog 投影后的命中。"""
        return (await self.resolve_with_audit(label)).match

    async def resolve_with_audit(self, label: str) -> PageNavAliasResolution:
        """按静态、精确、向量顺序解析，并返回不含异常详情的审计事实。"""
        repository_configured = self._repository is not None
        normalized_text = normalize_nav_alias_text(label)
        if not normalized_text:
            return PageNavAliasResolution(
                match=None,
                source="none",
                repository_configured=repository_configured,
            )

        static_match = resolve_page_nav_alias(label)
        if static_match is not None:
            return PageNavAliasResolution(
                match=static_match,
                source="static",
                repository_configured=repository_configured,
            )

        if not repository_configured:
            return PageNavAliasResolution(
                match=None,
                source="none",
                repository_configured=False,
            )
        if not self._repository_is_available():
            return PageNavAliasResolution(
                match=None,
                source="none",
                repository_configured=True,
                repository_degraded=True,
                degraded_stage="circuit_breaker",
                degraded_code="repository_temporarily_disabled",
            )

        try:
            exact_match = await self._find_confirmed_exact(normalized_text)
        except Exception:  # noqa: BLE001
            self._disable_repository_temporarily()
            logger.warning("page nav alias exact repository failed", exc_info=True)
            return PageNavAliasResolution(
                match=None,
                source="none",
                repository_configured=True,
                exact_lookup_attempted=True,
                repository_degraded=True,
                degraded_stage="exact",
                degraded_code="repository_query_failed",
            )
        if _is_catalog_projection(exact_match):
            assert exact_match is not None
            match = _with_match_type(exact_match, "exact")
            return PageNavAliasResolution(
                match=match,
                source="exact",
                repository_configured=True,
                exact_lookup_attempted=True,
            )

        try:
            vector_match = await self._find_confirmed_vector_match(label)
        except Exception:  # noqa: BLE001
            self._disable_repository_temporarily()
            logger.warning("page nav alias vector repository failed", exc_info=True)
            return PageNavAliasResolution(
                match=None,
                source="none",
                repository_configured=True,
                exact_lookup_attempted=True,
                vector_lookup_attempted=True,
                repository_degraded=True,
                degraded_stage="vector",
                degraded_code="repository_query_failed",
            )
        if _is_catalog_projection(vector_match):
            assert vector_match is not None
            match = _with_match_type(vector_match, "vector")
            return PageNavAliasResolution(
                match=match,
                source="vector",
                repository_configured=True,
                exact_lookup_attempted=True,
                vector_lookup_attempted=True,
            )
        return PageNavAliasResolution(
            match=None,
            source="none",
            repository_configured=True,
            exact_lookup_attempted=True,
            vector_lookup_attempted=True,
        )

    def _repository_is_available(self) -> bool:
        return self._repository is not None and time.monotonic() >= self._repository_disabled_until

    def _disable_repository_temporarily(self) -> None:
        self._repository_disabled_until = time.monotonic() + 60.0

    async def _find_confirmed_exact(
        self,
        normalized_text: str,
    ) -> PageNavAliasMatch | None:
        assert self._repository is not None
        return await self._repository.find_confirmed_exact(normalized_text)

    async def _find_confirmed_vector_match(self, label: str) -> PageNavAliasMatch | None:
        assert self._repository is not None
        return await self._repository.find_confirmed_vector_match(
            embedding=embed_nav_alias_text(label),
            embedding_model=PAGE_NAV_ALIAS_EMBEDDING_MODEL,
            min_similarity=self._vector_min_similarity,
        )


def normalize_nav_alias_text(value: str) -> str:
    """归一化可见导航文案，统一精确匹配和向量入库文本口径。"""
    normalized = re.sub(r"^text\s*=\s*", "", value.strip(), flags=re.IGNORECASE)
    normalized = normalized.strip("'\"")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.casefold()


def resolve_page_nav_alias(label: str) -> PageNavAliasMatch | None:
    """将正式 Catalog 导航别名解析到 canonical code。"""
    match = get_default_page_type_registry().resolve_alias(label)
    if match is None:
        return None
    return PageNavAliasMatch(
        canonical_code=match.canonical_code,
        page_subtype_code=match.page_subtype_code,
        matched_term=match.matched_term,
        source_category=match.source_category,
        confidence=match.confidence,
        match_type="static",
    )


def build_default_page_nav_alias_seed_rows() -> tuple[PageNavAliasSeedRow, ...]:
    """从唯一 Catalog 生成正式导航别名和本地 embedding 投影。"""
    rows: list[PageNavAliasSeedRow] = []
    registry = get_default_page_type_registry()
    for page_type in registry.catalog.page_types:
        for alias in page_type.navigation_aliases:
            for label in alias.terms:
                normalized_text = normalize_nav_alias_text(label)
                embedding_text = build_nav_alias_embedding_text(label=label)
                rows.append(
                    PageNavAliasSeedRow(
                        canonical_code=page_type.code,
                        page_subtype_code=alias.subtype,
                        label=label,
                        description=(
                            f"{alias.source_category}导航别名，映射到页面类型 {page_type.code}"
                        ),
                        normalized_text=normalized_text,
                        embedding_text=embedding_text,
                        source_category=alias.source_category,
                        confidence=alias.confidence,
                        embedding=embed_nav_alias_text(embedding_text),
                    )
                )
    return tuple(rows)


def build_nav_alias_embedding_text(*, label: str) -> str:
    """Return the local vector input text for an alias label."""
    return normalize_nav_alias_text(label)


def embed_nav_alias_text(value: str) -> tuple[float, ...]:
    """使用本地哈希特征生成固定维度向量，避免导航别名链路依赖远程模型。"""
    normalized = normalize_nav_alias_text(value)
    if not normalized:
        return tuple(0.0 for _ in range(PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS))

    vector = [0.0] * PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS
    for token in _nav_alias_embedding_tokens(normalized):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return tuple(0.0 for _ in range(PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS))
    return tuple(value / norm for value in vector)


def _nav_alias_embedding_tokens(normalized_text: str) -> tuple[str, ...]:
    chars = tuple(normalized_text)
    bigrams = tuple(
        normalized_text[index : index + 2] for index in range(max(len(normalized_text) - 1, 0))
    )
    return (normalized_text, *chars, *bigrams)


def _is_catalog_projection(match: PageNavAliasMatch | None) -> bool:
    if match is None:
        return False
    formal_match = resolve_page_nav_alias(match.matched_term)
    return formal_match is not None and formal_match.canonical_code == match.canonical_code


def _with_match_type(match: PageNavAliasMatch, match_type: str) -> PageNavAliasMatch:
    if match.match_type == match_type:
        return match
    return PageNavAliasMatch(
        canonical_code=match.canonical_code,
        page_subtype_code=match.page_subtype_code,
        matched_term=match.matched_term,
        source_category=match.source_category,
        confidence=match.confidence,
        match_type=match_type,
    )
