"""H5 Publication 可靠通知持久化迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260817_0064_h5_inspection_notification.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("h5_notification_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(method: str) -> str:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations
    getattr(migration, method)()
    return " ".join(operations.statements)


def test_migration_follows_current_unique_head() -> None:
    migration = _load_migration()

    assert migration.revision == "20260817_0064"
    assert migration.down_revision == "20260814_0063"


def test_upgrade_creates_h5_publication_rules_and_work_links() -> None:
    sql = _sql("upgrade")

    assert "CREATE TABLE qc_h5_inspection_publication" in sql
    assert "CREATE TABLE qc_h5_inspection_publication_rule" in sql
    assert "CREATE TABLE qc_h5_inspection_publication_work_item" in sql
    assert "UNIQUE (task_id, page_capture_id, finalization_epoch)" in sql
    assert "UNIQUE (input_hash)" in sql
    assert "REFERENCES qc_h5_page_type_assessment(assessment_record_id)" in sql
    assert "status IN ('pending', 'leased', 'retry_wait', 'published'" in sql
    assert "mode IN ('shadow', 'enforced')" in sql
    assert "attempt_count <= max_attempts" in sql
    assert "lease_token IS NOT NULL" in sql
    assert "UNIQUE (publication_id, batch_key)" in sql


def test_downgrade_refuses_audit_loss_and_drops_children_first() -> None:
    sql = _sql("downgrade")

    assert "cannot downgrade H5 inspection notification while audit rows exist" in sql
    assert sql.index("DROP TABLE qc_h5_inspection_publication_work_item") < sql.rindex(
        "DROP TABLE qc_h5_inspection_publication"
    )
    assert sql.index("DROP TABLE qc_h5_inspection_publication_rule") < sql.rindex(
        "DROP TABLE qc_h5_inspection_publication"
    )
