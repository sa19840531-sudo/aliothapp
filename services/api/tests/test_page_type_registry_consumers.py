"""Registry 消费者不能维护固定页面类型集合。"""

from __future__ import annotations

import json
from pathlib import Path

from services.api.app.agents.tools import exploration_tools
from services.api.app.application import page_exploration_summary
from services.api.app.domain.page.registry import PageTypeRegistry


def _registry_with_extra_types(tmp_path: Path) -> PageTypeRegistry:
    source = Path(__file__).parents[1] / "app" / "domain" / "page" / "page_type_catalog.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    business = dict(payload["page_types"][1])
    business.update(
        {
            "code": "search",
            "name": "搜索页",
            "aliases": ["search"],
            "coverage_required": True,
            "entry_hints": [],
            "next_page_candidates": [],
            "navigation_aliases": [],
            "route_prefixes": [],
        }
    )
    lifecycle = dict(payload["page_types"][0])
    lifecycle.update(
        {
            "code": "interstitial",
            "name": "过渡页",
            "aliases": ["interstitial"],
            "runtime_traits": [],
        }
    )
    payload["page_types"].extend([business, lifecycle])
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return PageTypeRegistry.from_path(path)


def test_exploration_closure_missing_types_follow_registry(tmp_path: Path) -> None:
    """新增业务类型后仍用固定 12 类时应失败。"""
    registry = _registry_with_extra_types(tmp_path)

    missing = exploration_tools.missing_coverage_page_types(
        registry.coverage_required_codes[:-1],
        registry=registry,
    )

    assert missing == ["search"]


def test_exploration_summary_lifecycle_types_follow_registry(tmp_path: Path) -> None:
    """新增生命周期类型后仍用固定二元集合时应失败。"""
    registry = _registry_with_extra_types(tmp_path)

    assert page_exploration_summary.lifecycle_page_types(registry=registry) == {
        "line_loading",
        "startup_ad",
        "guide_page",
        "interstitial",
    }
