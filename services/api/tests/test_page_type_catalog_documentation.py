"""运行时 Skill 与当前说明文档不得复制页面类型目录。"""

from __future__ import annotations

import re
from pathlib import Path

from services.api.app.domain.page.registry import get_default_page_type_registry

API_ROOT = Path(__file__).parents[1]
RUNTIME_CONTRACT_PATHS = (
    API_ROOT / "app" / "agents" / "skills" / "phase" / "page-detection" / "SKILL.md",
    API_ROOT / "app" / "agents" / "skills" / "phase" / "evidence-report" / "SKILL.md",
    API_ROOT / "app" / "agents" / "skills" / "qc-workflow" / "SKILL.md",
    API_ROOT / "app" / "agents" / "tools" / "README.md",
    API_ROOT / "app" / "domain" / "page" / "README.md",
)
FIXED_PAGE_TYPE_COUNT = re.compile(
    r"\b\d+\s*(?:类|种|个)[^\n]*(?:页面|canonical code)",
    flags=re.IGNORECASE,
)
FIXED_CATALOG_VERSION = re.compile(r"\b20\d{2}-\d{2}-\d{2}\.v\d+\b")


def test_runtime_contracts_do_not_copy_catalog_codes_or_fixed_counts() -> None:
    """运行说明只能引用 Registry 派生集合，不能维护完整清单或固定数量。"""
    catalog_codes = set(get_default_page_type_registry().all_codes)

    for path in RUNTIME_CONTRACT_PATHS:
        text = path.read_text(encoding="utf-8")
        mentioned_codes = {code for code in catalog_codes if f"`{code}`" in text}
        assert mentioned_codes != catalog_codes, path
        assert FIXED_PAGE_TYPE_COUNT.search(text) is None, path
        assert FIXED_CATALOG_VERSION.search(text) is None, path


def test_page_detection_skill_documents_catalog_audit_fields() -> None:
    text = RUNTIME_CONTRACT_PATHS[0].read_text(encoding="utf-8")

    assert "catalog_version" in text
    assert "catalog_sha256" in text
