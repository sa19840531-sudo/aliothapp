"""0053 App Run 执行租约迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0054_app_run_execution_lease.py"
)


class RecordingOperations:
    """只记录迁移 SQL，避免合同测试依赖真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_run_execution_lease_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_app_run_lease_with_identity_and_runtime_guards() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260814_0054"
    assert migration.down_revision == "20260814_0053"
    assert "CREATE TABLE qc_app_run_execution_lease" in sql
    assert (
        "agent_run_id uuid PRIMARY KEY REFERENCES qc_agent_run(id) ON DELETE CASCADE"
        in sql
    )
    assert "task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE" in sql
    assert "lease_owner varchar(255) NOT NULL" in sql
    assert "lease_token uuid NOT NULL" in sql
    assert "lease_expires_at timestamptz NOT NULL" in sql
    assert "row_version bigint NOT NULL DEFAULT 0" in sql
    assert "attempt_count integer NOT NULL DEFAULT 0" in sql
    assert "BTRIM(lease_owner) <> ''" in sql
    assert "row_version >= 0" in sql
    assert "attempt_count >= 0" in sql
    assert "CREATE OR REPLACE FUNCTION qc_guard_app_run_execution_lease()" in sql
    assert "run.task_id = NEW.task_id" in sql
    assert "task.target_platform = 'app'" in sql
    assert "FOR UPDATE OF run" in sql
    assert "run_status IN ('done', 'failed', 'aborted', 'canceled')" in sql
    assert "NEW.lease_expires_at <= now()" in sql
    assert "终态 Agent Run 不允许新建 App 执行租约" in sql
    assert "NEW.row_version <> OLD.row_version + 1" in sql
    assert "OLD.lease_expires_at > now()" in sql
    assert "活跃 App Run 执行租约禁止被其他持有者接管" in sql
    assert "NEW.attempt_count <> OLD.attempt_count + 1" in sql
    assert "OLD.lease_expires_at <= now()" in sql
    assert "已过期 App Run 执行租约不得以原 token 恢复续租" in sql
    assert "同一 App Run 执行租约续租不得改变 attempt_count" in sql
    assert "NEW.lease_owner IS DISTINCT FROM OLD.lease_owner" in sql
    assert "NEW.lease_token IS DISTINCT FROM OLD.lease_token" in sql
    assert "NEW.attempt_count IS DISTINCT FROM OLD.attempt_count" in sql
    assert "终态 Agent Run 只允许原持有者续租以完成终态交接" in sql
    assert "trg_qc_app_run_execution_lease_guard" in sql
    assert "BEFORE INSERT OR UPDATE ON qc_app_run_execution_lease" in sql
    assert "trg_qc_app_run_execution_lease_updated_at" in sql
    assert "set_updated_at()" in sql


def test_downgrade_fails_closed_for_active_or_stale_lease_rows() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    active_guard = sql.index("lease_expires_at > now()")
    drop_table = sql.index("DROP TABLE IF EXISTS qc_app_run_execution_lease")
    assert active_guard < drop_table
    assert "存在活跃 App Run 执行租约，禁止降级 0053" in sql
    assert "存在已过期 App Run 执行租约，禁止静默清理后降级 0053" in sql
    assert "DROP TRIGGER IF EXISTS trg_qc_app_run_execution_lease_guard" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_app_run_execution_lease()" in sql
