"""正式导航别名只能由页面类型 Catalog 决定。"""

from __future__ import annotations

from services.api.app.domain.page.nav_alias import (
    PageNavAliasMatch,
    PageNavAliasResolver,
)


class ConflictingAliasRepository:
    async def find_confirmed_exact(self, normalized_text: str) -> PageNavAliasMatch | None:
        return PageNavAliasMatch(
            canonical_code="profile",
            page_subtype_code="account_center",
            matched_term=normalized_text,
            source_category="数据库冲突候选",
            confidence=1.0,
            match_type="exact",
        )

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        return None


class VectorAliasRepository:
    async def find_confirmed_exact(self, normalized_text: str) -> PageNavAliasMatch | None:
        return None

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        return PageNavAliasMatch(
            canonical_code="content_list",
            page_subtype_code="comic_anime",
            matched_term="漫画",
            source_category="漫画",
            confidence=0.88,
            match_type="vector",
        )


class FailingExactAliasRepository:
    async def find_confirmed_exact(self, normalized_text: str) -> PageNavAliasMatch | None:
        raise RuntimeError("不得进入审计载荷的数据库详情")

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        raise AssertionError("精确查询失败后不得继续访问向量仓储")


async def test_catalog_alias_wins_over_conflicting_database_projection() -> None:
    """数据库确认数据覆盖 Catalog 正式别名时应失败。"""
    resolver = PageNavAliasResolver(repository=ConflictingAliasRepository())

    match = await resolver.resolve("短视频")

    assert match is not None
    assert match.canonical_code == "video_list"
    assert match.page_subtype_code == "short_video"
    assert match.match_type == "static"



async def test_vector_resolution_exposes_safe_audit_facts() -> None:
    resolver = PageNavAliasResolver(repository=VectorAliasRepository())
    resolve_with_audit = getattr(resolver, "resolve_with_audit", None)

    assert resolve_with_audit is not None
    resolution = await resolve_with_audit("神秘短剧")

    assert resolution.match is not None
    assert resolution.match.canonical_code == "content_list"
    assert resolution.source == "vector"
    assert resolution.repository_configured is True
    assert resolution.exact_lookup_attempted is True
    assert resolution.vector_lookup_attempted is True
    assert resolution.repository_degraded is False
    assert resolution.degraded_stage is None
    assert resolution.degraded_code is None


async def test_repository_failure_is_fixed_safe_degradation_audit() -> None:
    resolver = PageNavAliasResolver(repository=FailingExactAliasRepository())
    resolve_with_audit = getattr(resolver, "resolve_with_audit", None)

    assert resolve_with_audit is not None
    resolution = await resolve_with_audit("神秘短剧")

    assert resolution.match is None
    assert resolution.source == "none"
    assert resolution.repository_degraded is True
    assert resolution.degraded_stage == "exact"
    assert resolution.degraded_code == "repository_query_failed"
    assert "数据库详情" not in repr(resolution)
