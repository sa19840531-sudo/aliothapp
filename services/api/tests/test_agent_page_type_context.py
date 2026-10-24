"""Main Agent 页面类型上下文必须由 Registry 动态生成。"""

from __future__ import annotations

import json
from pathlib import Path

from services.api.app.agents import instructions
from services.api.app.domain.page.registry import PageTypeRegistry


def _registry_with_search_page(tmp_path: Path) -> PageTypeRegistry:
    source = Path(__file__).parents[1] / "app" / "domain" / "page" / "page_type_catalog.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    search = dict(payload["page_types"][1])
    search.update(
        {
            "code": "search",
            "name": "搜索页",
            "aliases": ["search"],
            "entry_hints": [],
            "next_page_candidates": [],
            "navigation_aliases": [],
            "route_prefixes": ["search"],
        }
    )
    payload["page_types"].append(search)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return PageTypeRegistry.from_path(path)


def test_main_agent_instructions_inject_registry_context(tmp_path: Path) -> None:
    """新增页面类型后仍使用固定 Prompt 清单时应失败。"""
    registry = _registry_with_search_page(tmp_path)

    text = instructions.build_main_agent_instructions(registry=registry)

    assert registry.content_sha256 in text
    assert '"coverage_required_page_types"' in text
    assert '"search"' in text
    assert "运行时页面类型目录" in text
