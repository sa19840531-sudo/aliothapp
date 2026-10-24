"""Web 规范页面类型数据库兼容迁移的结构契约测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260904_0089_web_canonical_page_types.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "web_canonical_page_types_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_accepts_v2_coverage_and_canonical_menu_page_types() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260904_0089"
    assert migration.down_revision == "20260903_0088"
    assert "DROP CONSTRAINT ck_qc_web_coverage_snapshot_normalization" in sql
    assert "web-page-type-normalization.v1" in sql
    assert "web-page-type-assessment.v2" in sql
    assert "DROP CONSTRAINT ck_qc_web_menu_success_evidence" in sql
    assert "menu_page_type IN ( 'content_list', 'video_list'," in sql
    assert "menu_page_type IN ( 'content_detail', 'video_detail'," in sql
    assert "detail_page_type IN ( 'content_detail', 'video_detail'," in sql
    assert "menu_page_type IN ( 'home', 'content_list'," in sql


def test_downgrade_preserves_historical_v2_snapshots_and_legacy_menu_rows() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "web-page-type-assessment.v2" in sql
    assert "web_content_list" in sql
    assert "web_video_list" in sql
    assert "web_content_detail" in sql
    assert "web_video_detail" in sql
