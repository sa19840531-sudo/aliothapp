"""校验封存 Capture 质检 overlay 守卫迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260820_0074_allow_sealed_capture_inspection_overlay.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "allow_sealed_capture_inspection_overlay_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_allows_inspection_overlay_only() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260820_0074"
    assert migration.down_revision == "20260820_0073"
    assert "qc_guard_sealed_app_evidence_capture" in sql
    assert "- 'inspection'" in sql
    assert "- 'inspection_status'" in sql
    assert "- 'page_type'" in sql
    assert "- 'page_state'" in sql
    assert "App Evidence Index 已封存，禁止追加、修改或删除 Capture" in sql


def test_detection_status_overlay_migration_allows_recognized_field() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260824_0077_sealed_capture_detection_status_overlay.py"
    )
    spec = importlib.util.spec_from_file_location(
        "sealed_capture_detection_status_overlay", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operations = RecordingOperations()
    module.op = operations
    module.upgrade()
    sql = " ".join(operations.statements)
    assert module.revision == "20260824_0077"
    assert "- 'detection_status'" in sql
    assert "- 'page_type'" in sql


def test_downgrade_restores_strict_metadata_guard() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "NEW.metadata = OLD.metadata" in sql
    assert "NEW.updated_at = OLD.updated_at" in sql
    assert "- 'inspection'" not in sql
