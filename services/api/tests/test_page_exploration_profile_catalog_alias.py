"""H5 探索标签映射必须复用 Catalog 正式别名。"""

from __future__ import annotations

from services.api.app.application.page_exploration_profile import (
    _expected_page_type_from_label,
)


def test_expected_page_type_uses_catalog_alias_not_local_keyword_table() -> None:
    """Catalog 已支持但旧本地关键词表缺失的别名应能解析。"""
    assert _expected_page_type_from_label("91娱乐") == "home"
    assert _expected_page_type_from_label("AI女友") == "content_list"
