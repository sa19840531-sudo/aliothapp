from __future__ import annotations

from typing import Any

import pytest

from services.api.app.domain.page.nav_alias import (
    PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS,
    PageNavAliasMatch,
    PageNavAliasResolver,
    build_default_page_nav_alias_seed_rows,
    resolve_page_nav_alias,
)


class FakeAliasRepository:
    def __init__(
        self,
        *,
        exact: PageNavAliasMatch | None = None,
        vector: PageNavAliasMatch | None = None,
    ) -> None:
        self.exact = exact
        self.vector = vector
        self.exact_calls: list[str] = []
        self.vector_calls: list[dict[str, Any]] = []

    async def find_confirmed_exact(self, normalized_text: str) -> PageNavAliasMatch | None:
        self.exact_calls.append(normalized_text)
        return self.exact

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        self.vector_calls.append(
            {
                "embedding": embedding,
                "embedding_model": embedding_model,
                "min_similarity": min_similarity,
            }
        )
        return self.vector


@pytest.mark.asyncio
async def test_nav_alias_resolver_rejects_unlisted_exact_repository_match() -> None:
    repository = FakeAliasRepository(
        exact=PageNavAliasMatch(
            canonical_code="content_list",
            page_subtype_code="short_drama",
            matched_term="神秘短剧",
            source_category="短剧",
            confidence=0.96,
            match_type="exact",
        )
    )
    resolver = PageNavAliasResolver(repository=repository)

    match = await resolver.resolve("神秘短剧")

    assert match is None
    assert repository.exact_calls == ["神秘短剧"]


@pytest.mark.asyncio
async def test_nav_alias_resolver_rejects_unlisted_vector_repository_match() -> None:
    repository = FakeAliasRepository(
        vector=PageNavAliasMatch(
            canonical_code="profile",
            page_subtype_code="account_center",
            matched_term="私人库房",
            source_category="个人中心",
            confidence=0.84,
            match_type="vector",
        )
    )
    resolver = PageNavAliasResolver(repository=repository, vector_min_similarity=0.8)

    match = await resolver.resolve("私密仓库")

    assert match is None
    assert repository.exact_calls == ["私密仓库"]
    assert len(repository.vector_calls) == 1


@pytest.mark.asyncio
async def test_shared_navigation_alias_lookup_keeps_point_eight_confidence_floor() -> None:
    repository = FakeAliasRepository()

    await PageNavAliasResolver(repository=repository).resolve("未登记菜单链接")

    assert repository.vector_calls[0]["min_similarity"] == pytest.approx(0.8)


def test_app_and_h5_alias_factories_keep_shared_point_eight_default() -> None:
    from services.api.app.agents.tools.h5_visual_navigation_detection import (
        _get_visual_navigation_alias_resolver,
    )
    from services.api.app.agents.tools.page_detection_tools import (
        _get_page_nav_alias_resolver,
    )

    assert _get_page_nav_alias_resolver()._vector_min_similarity == pytest.approx(0.8)
    assert _get_visual_navigation_alias_resolver()._vector_min_similarity == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_nav_alias_resolver_prefers_catalog_before_database_projection() -> None:
    repository = FakeAliasRepository()
    resolver = PageNavAliasResolver(repository=repository)

    match = await resolver.resolve("91私库")

    assert match is not None
    assert match.canonical_code == "profile"
    assert match.match_type == "static"
    assert repository.exact_calls == []
    assert repository.vector_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "expected_page_type"),
    [
        ("发布", "post_publish"),
    ],
)
async def test_nav_alias_resolver_includes_rule_target_entries(
    label: str,
    expected_page_type: str,
) -> None:
    resolver = PageNavAliasResolver(repository=FakeAliasRepository())

    match = await resolver.resolve(label)

    assert match is not None
    assert match.canonical_code == expected_page_type
    assert match.match_type == "static"


def test_default_page_nav_alias_seed_rows_include_local_embeddings_and_descriptions() -> None:
    rows = build_default_page_nav_alias_seed_rows()

    assert rows
    first = rows[0]
    assert first.description
    assert first.embedding_text
    assert len(first.embedding) == PAGE_NAV_ALIAS_EMBEDDING_DIMENSIONS
    assert abs(sum(value * value for value in first.embedding) - 1.0) < 0.0001


@pytest.mark.parametrize(
    ("label", "expected_page_type"),
    [
        ("系统设置", "settings"),
        ("发布作品", "post_publish"),
    ],
)
def test_static_nav_aliases_cover_nested_business_page_entries(
    label: str,
    expected_page_type: str,
) -> None:
    match = resolve_page_nav_alias(label)

    assert match is not None
    assert match.canonical_code == expected_page_type
