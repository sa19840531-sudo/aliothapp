"""真实 PostgreSQL 集成测试的数据库隔离防线。"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit


def _postgres_identity(database_url: str) -> tuple[str, str, int, str, str | None]:
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("数据库连接地址不能为空")
    parsed = urlsplit(database_url.strip())
    scheme = parsed.scheme.casefold()
    if scheme not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError("数据库连接地址必须使用 PostgreSQL scheme")
    try:
        port = parsed.port or 5432
    except ValueError as error:
        raise ValueError("数据库连接端口非法") from error
    host = (parsed.hostname or "").casefold()
    database_name = unquote(parsed.path).strip("/")
    if not host or not database_name or "/" in database_name:
        raise ValueError("数据库连接地址必须包含 host 和单一 database 名称")
    role = unquote(parsed.username) if parsed.username else None
    return "postgresql", host, port, database_name, role


def normalize_postgres_database_url(database_url: str) -> str:
    """按 scheme/host/port/database 归一化，忽略凭据与 query 差异。"""
    scheme, host, port, database_name, _ = _postgres_identity(database_url)
    display_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{display_host}:{port}/{database_name}"


def require_isolated_test_database_url(
    test_database_url: str,
    primary_database_url: str | None,
    *,
    allowed_database_names: frozenset[str] | None = None,
    allowed_roles: frozenset[str] | None = None,
) -> str:
    """拒绝测试库与主库同址，调用方仍需显式提供测试连接地址。"""
    normalized_test = normalize_postgres_database_url(test_database_url)
    if (
        primary_database_url is not None
        and normalize_postgres_database_url(primary_database_url) == normalized_test
    ):
        raise ValueError("ALIOTH_TEST_DATABASE_URL 不能与 ALIOTH_DATABASE_URL 指向同一数据库")
    _, _, _, database_name, role = _postgres_identity(test_database_url)
    if allowed_database_names is not None and database_name not in allowed_database_names:
        raise ValueError("测试数据库不在显式允许列表")
    if allowed_roles is not None and role not in allowed_roles:
        raise ValueError("测试数据库角色不在显式允许列表")
    return test_database_url
