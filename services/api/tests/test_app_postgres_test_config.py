"""真实 PostgreSQL 集成测试的纯配置防线测试。"""

from __future__ import annotations

import pytest

from services.api.app.infrastructure.db.test_database_guard import (
    normalize_postgres_database_url,
    require_isolated_test_database_url,
)


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        (
            "postgresql://tester:secret@db.example:5432/alioth_test",
            "postgresql://db.example:5432/alioth_test",
        ),
        (
            " postgresql+asyncpg://tester:secret@db.example:5432/alioth_test/ ",
            "postgresql://db.example:5432/alioth_test",
        ),
        (
            "postgresql://tester:new-secret@DB.EXAMPLE/alioth_test?ssl=require",
            "postgresql://db.example:5432/alioth_test",
        ),
    ],
)
def test_normalize_postgres_database_url(database_url: str, expected: str) -> None:
    assert normalize_postgres_database_url(database_url) == expected


def test_require_isolated_test_database_url_rejects_same_database() -> None:
    with pytest.raises(ValueError, match="不能与 ALIOTH_DATABASE_URL 指向同一数据库"):
        require_isolated_test_database_url(
            "postgresql+asyncpg://tester:secret@db.example:5432/alioth_test",
            "postgresql://tester:secret@db.example:5432/alioth_test/",
        )


def test_require_isolated_test_database_url_rejects_same_identity_with_other_credentials() -> None:
    with pytest.raises(ValueError, match="指向同一数据库"):
        require_isolated_test_database_url(
            "postgresql://test_role:new@db.example/alioth_test?ssl=require",
            "postgresql+asyncpg://prod_role:old@DB.EXAMPLE:5432/alioth_test",
        )


def test_require_isolated_test_database_url_enforces_optional_allowlists() -> None:
    with pytest.raises(ValueError, match="数据库不在显式允许列表"):
        require_isolated_test_database_url(
            "postgresql://tester:secret@db.example/alioth_test",
            None,
            allowed_database_names=frozenset({"alioth_ci"}),
        )
    with pytest.raises(ValueError, match="角色不在显式允许列表"):
        require_isolated_test_database_url(
            "postgresql://developer:secret@db.example/alioth_test",
            None,
            allowed_roles=frozenset({"tester"}),
        )


def test_require_isolated_test_database_url_allows_different_database() -> None:
    database_url = "postgresql://tester:secret@db.example:5432/alioth_test"

    assert (
        require_isolated_test_database_url(
            database_url,
            "postgresql://tester:secret@db.example:5432/alioth",
        )
        == database_url
    )
