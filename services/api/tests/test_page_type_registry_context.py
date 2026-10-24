"""页面类型 Registry 上下文与摘要测试。"""

from __future__ import annotations

import json
from pathlib import Path

from services.api.app.domain.page.registry import PageTypeRegistry


def _default_payload() -> dict:
    catalog_path = (
        Path(__file__).parents[1] / "app" / "domain" / "page" / "page_type_catalog.json"
    )
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def test_catalog_digest_ignores_json_formatting(tmp_path: Path) -> None:
    """摘要若依赖空格或换行应失败，保证审计值描述语义内容。"""
    payload = _default_payload()
    compact_path = tmp_path / "compact.json"
    pretty_path = tmp_path / "pretty.json"
    compact_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    pretty_path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")

    compact = PageTypeRegistry.from_path(compact_path)
    pretty = PageTypeRegistry.from_path(pretty_path)

    assert compact.content_sha256 == pretty.content_sha256
    assert compact.content_sha256.startswith("sha256:")
    assert len(compact.content_sha256) == 71


def test_agent_context_uses_registry_derived_groups() -> None:
    """删除动态 Agent 上下文时应失败，避免 Prompt 维护第二套类型清单。"""
    registry = PageTypeRegistry.from_path(
        Path(__file__).parents[1] / "app" / "domain" / "page" / "page_type_catalog.json"
    )

    context = registry.build_agent_context()

    assert context["catalog_version"] == "2026-09-11.v15"
    assert context["catalog_sha256"] == registry.content_sha256
    assert context["coverage_required_page_types"] == list(registry.coverage_required_codes)
    assert context["lifecycle_page_types"] == [
        "line_loading",
        "startup_ad",
        "guide_page",
    ]
