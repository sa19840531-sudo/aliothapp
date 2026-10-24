"""页面类型 Registry 的别名、路由和详情解析测试。"""

from __future__ import annotations

from services.api.app.domain.page.registry import get_default_page_type_registry


def test_registry_resolves_formal_navigation_alias() -> None:
    """正式别名未由 Catalog 提供时应失败。"""
    match = get_default_page_type_registry().resolve_alias("短视频")

    assert match is not None
    assert match.canonical_code == "video_list"
    assert match.page_subtype_code == "short_video"


def test_registry_resolves_site_navigation_aliases() -> None:
    registry = get_default_page_type_registry()

    forbidden_zone = registry.resolve_alias("禁区")
    two_dimensional = registry.resolve_alias("二次元")
    cape = registry.resolve_alias("海角")
    square = registry.resolve_alias("广场")
    planet = registry.resolve_alias("星球")
    resource = registry.resolve_alias("资源")
    moments = registry.resolve_alias("朋友圈")

    assert forbidden_zone is not None
    assert forbidden_zone.canonical_code == "video_list"
    assert two_dimensional is not None
    assert two_dimensional.canonical_code == "home"
    assert cape is not None
    assert cape.canonical_code == "post_list"
    assert square is not None
    assert square.canonical_code == "post_list"
    assert planet is not None
    assert planet.canonical_code == "post_list"
    assert resource is not None
    assert resource.canonical_code == "post_list"
    assert moments is not None
    assert moments.canonical_code == "content_list"


def test_registry_resolves_route_prefix() -> None:
    """业务路由仍由页面识别工具硬编码时应失败。"""
    registry = get_default_page_type_registry()

    assert registry.resolve_route_prefix("movies") == "video_list"
    assert registry.resolve_route_prefix("shortVideo") == "video_list"
    assert registry.resolve_route_prefix("COMICS") == "content_list"
    assert registry.resolve_route_prefix("main_page") == "home"
    assert registry.resolve_route_prefix("post") == "content_list"
    assert registry.resolve_route_prefix("missing") is None


def test_registry_resolves_default_detail_type() -> None:
    """列表到详情映射仍由页面识别工具推断时应失败。"""
    registry = get_default_page_type_registry()

    assert registry.default_detail_type("video_list") == "video_detail"
    assert registry.default_detail_type("post_list") == "content_detail"
    assert registry.default_detail_type("home") is None
