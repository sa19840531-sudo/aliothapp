"""H5 动态覆盖目标与 Web 固定覆盖目标的入口合同。"""

from __future__ import annotations

from services.api.app.api.v2.schemas import (
    AppExplorationTargetRequest,
    H5ExplorationTargetRequest,
    WebExplorationTargetRequest,
)


def test_app_and_h5_request_snapshots_are_unchanged_by_web_v4_partition() -> None:
    assert AppExplorationTargetRequest(
        required_page_types=["home"],
        optional_page_types=["profile"],
        lifecycle_page_types=["line_loading"],
    ).model_dump() == {
        "required_page_types": ["home"],
        "optional_page_types": ["profile"],
        "lifecycle_page_types": ["line_loading"],
        "applicability_mode": None,
        "milestones": [],
        "minimum_business_pages": None,
        "max_actions": None,
        "device_capability_template": "android_visual_readonly_v1",
    }
    assert H5ExplorationTargetRequest(
        required_page_types=["home", "profile"]
    ).model_dump() == {"required_page_types": ["home", "profile"]}


def test_web_request_accepts_only_seven_business_page_types() -> None:
    assert WebExplorationTargetRequest(
        required_page_types=["activity", "search_page"]
    ).required_page_types == ["activity", "search_page"]

    for page_type in ("age_gate", "unknown"):
        from pydantic import ValidationError

        try:
            WebExplorationTargetRequest(required_page_types=[page_type])
        except ValidationError:
            continue
        raise AssertionError(f"{page_type} 不得进入 Web 业务分母")


def test_h5_default_keeps_dynamic_discovery_scope() -> None:
    """不传目标时不能把全量 Catalog 伪造成单个产品必须页面。"""
    assert H5ExplorationTargetRequest().required_page_types == []


def test_h5_explicit_empty_still_means_unlocked() -> None:
    """显式传空列表仍表示不锁定分母、采到啥算啥。"""
    assert H5ExplorationTargetRequest(required_page_types=[]).required_page_types == []


def test_h5_explicit_subset_is_preserved() -> None:
    """显式指定时按指定，不被默认值覆盖。"""
    target = H5ExplorationTargetRequest(required_page_types=["home", "profile"])

    assert target.required_page_types == ["home", "profile"]


def test_web_default_scope_is_resolved_by_site_catalog_at_submission() -> None:
    """Web 请求默认不伪造分母；提交入口按站点目录冻结四类业务页型。"""
    assert WebExplorationTargetRequest().required_page_types == []
