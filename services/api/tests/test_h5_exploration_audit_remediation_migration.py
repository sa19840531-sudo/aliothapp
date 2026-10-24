"""H5 页面评估与证据库存迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0057_h5_exploration_audit_remediation.py"
)


class RecordingOperations:
    """记录迁移 SQL，验证升级和安全降级的可观察合同。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "h5_exploration_audit_remediation_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_owned_assessment_and_inventory_facts() -> None:
    """若删掉任一归属复合外键或 JSON/终态约束，此测试必须失败。"""
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260814_0057"
    assert migration.down_revision == "20260814_0056"
    assert "CREATE TABLE qc_h5_page_type_assessment" in sql
    assert "CREATE TABLE qc_h5_evidence_inventory_item" in sql
    assert "FOREIGN KEY (task_id, agent_run_id)" in sql
    assert "FOREIGN KEY (task_id, session_id, action_execution_id)" in sql
    assert "UNIQUE (page_capture_id, schema_version)" in sql
    assert "jsonb_typeof(reason_codes) = 'array'" in sql
    assert "status IN ('pass', 'insufficient', 'failed')" in sql
    assert "uq_qc_agent_run_task_id_id" in sql
    assert "uq_qc_page_capture_task_id_id" in sql
    assert "uq_qc_capture_task_id_id" in sql
    assert "uq_qc_action_execution_task_session_id" in sql


def test_downgrade_refuses_to_delete_nonempty_h5_audit_facts() -> None:
    """若降级静默删除新事实或未按依赖逆序清理索引，此测试必须失败。"""
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "RAISE EXCEPTION" in sql
    assert "qc_h5_page_type_assessment contains rows" in sql
    assert "qc_h5_evidence_inventory_item contains rows" in sql
    assert sql.index("DROP TABLE qc_h5_evidence_inventory_item") < sql.index(
        "DROP TABLE qc_h5_page_type_assessment"
    )
    assert sql.index("DROP TABLE qc_h5_page_type_assessment") < sql.index(
        "DROP INDEX uq_qc_agent_run_task_id_id"
    )
