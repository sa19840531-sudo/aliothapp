"""H5/Web Capture 公共身份角色对齐迁移测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0048_h5_capture_role_alignment.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "h5_capture_role_alignment_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_aligns_common_web_capture_role_and_trigger() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260812_0048"
    assert migration.down_revision == "20260812_0047"
    assert "capture_role NOT IN" in sql
    assert "'probe_before'" in sql
    assert "'probe_result_replay'" in sql
    assert "DROP CONSTRAINT ck_qc_capture_role" in sql
    assert "UPDATE qc_capture AS common_capture" in sql
    assert "capture_role = page_capture.capture_role" in sql
    assert "NEW.capture_role" in sql


def test_downgrade_restores_legacy_observation_projection() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "capture_role = 'observation'" in sql
    assert "'bootstrap', 'before_action', 'after_action', 'observation', 'supplemental'" in sql
