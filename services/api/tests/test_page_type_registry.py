"""页面类型 Registry 的领域契约测试。"""

from __future__ import annotations

import importlib


def test_default_registry_derives_page_type_groups_from_catalog() -> None:
    """删除 Catalog 分类派生时应失败，避免运行时重新维护页面类型集合。"""
    registry_module = importlib.import_module("services.api.app.domain.page.registry")

    registry = registry_module.get_default_page_type_registry()

    assert registry.lifecycle_codes == ("line_loading", "startup_ad", "guide_page")
    assert registry.coverage_required_codes == (
        "home",
        "content_list",
        "content_detail",
        "profile",
        "settings",
        "payment",
        "post_list",
        "post_publish",
        "activity",
        "video_list",
        "video_detail",
    )
    assert registry.transient_codes == ("line_loading",)
    assert "startup_ad" in registry.rule_matchable_codes
