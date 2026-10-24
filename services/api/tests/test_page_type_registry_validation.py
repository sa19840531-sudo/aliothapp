"""页面类型 Registry 的校验与查询测试。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _default_payload() -> dict:
    catalog_path = (
        Path(__file__).parents[1] / "app" / "domain" / "page" / "page_type_catalog.json"
    )
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def test_registry_exposes_catalog_order_and_strict_code_lookup() -> None:
    """删除严格查询或改变 Catalog 顺序时应失败。"""
    registry_module = importlib.import_module("services.api.app.domain.page.registry")
    registry = registry_module.get_default_page_type_registry()

    assert registry.all_codes[:3] == ("line_loading", "home", "content_list")
    assert registry.business_codes[0] == "home"
    assert registry.require_code("video_detail").code == "video_detail"

    with pytest.raises(ValueError, match="unknown"):
        registry.require_code("unknown")


def test_registry_rejects_unknown_detail_reference(tmp_path: Path) -> None:
    """删除跨页面引用校验时应失败。"""
    payload = _default_payload()
    payload["page_types"][2]["default_detail_page_type"] = "missing_detail"
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    registry_module = importlib.import_module("services.api.app.domain.page.registry")
    with pytest.raises(ValueError, match="missing_detail"):
        registry_module.PageTypeRegistry.from_path(path)


def test_registry_rejects_duplicate_page_type_code(tmp_path: Path) -> None:
    """删除 canonical code 唯一性校验时应失败。"""
    payload = _default_payload()
    payload["page_types"][1]["code"] = payload["page_types"][0]["code"]
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    registry_module = importlib.import_module("services.api.app.domain.page.registry")
    with pytest.raises(ValueError, match="重复"):
        registry_module.PageTypeRegistry.from_path(path)
