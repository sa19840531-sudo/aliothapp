"""页面类型目录只能经由 Registry 被业务模块消费。"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "app"
COMPATIBILITY_MODULES = {
    APP_ROOT / "domain" / "page" / "__init__.py",
    APP_ROOT / "domain" / "page" / "catalog.py",
    APP_ROOT / "domain" / "page" / "registry.py",
    # Web 五类闭集是页面目录领域层的独立受控加载器，不是绕过 Registry
    # 的业务模块；业务代码仍只能消费这里验证过的只读 Catalog。
    APP_ROOT / "domain" / "page" / "web_page_catalog.py",
}


def test_business_modules_do_not_import_default_catalog_loader() -> None:
    """除兼容层与 Registry 外，生产代码不得绕过统一出口加载默认 Catalog。"""
    violations: list[str] = []

    for path in APP_ROOT.rglob("*.py"):
        if path in COMPATIBILITY_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports_compatibility_loader = node.module in {
                    "services.api.app.domain.page",
                    "services.api.app.domain.page.catalog",
                } and any(
                    alias.name == "load_default_page_type_catalog" for alias in node.names
                )
                if imports_compatibility_loader:
                    violations.append(str(path.relative_to(APP_ROOT)))
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and Path(node.value).name == "page_type_catalog.json"
            ):
                violations.append(str(path.relative_to(APP_ROOT)))

    assert violations == []
