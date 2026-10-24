"""页面类型领域包的统一出口测试。"""

from __future__ import annotations

import services.api.app.domain.page as page_domain


def test_page_domain_exports_default_registry() -> None:
    """删除领域包统一出口时应失败，防止调用方依赖实现文件路径。"""
    registry = page_domain.get_default_page_type_registry()

    assert isinstance(registry, page_domain.PageTypeRegistry)
