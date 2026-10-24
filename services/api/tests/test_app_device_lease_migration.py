"""0058 App 设备资源与 fencing 租约迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0058_app_device_resource_lease.py"
)


class RecordingOperations:
    """只记录迁移 SQL，避免合同测试依赖真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_device_resource_lease_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_resource_lease_and_surface_fencing_contract() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260814_0058"
    assert migration.down_revision == "20260814_0057"
    assert "CREATE TABLE qc_device_resource" in sql
    assert (
        "health_state IN ('available', 'claimed', 'recovering', 'quarantined', 'disabled')"
        in sql
    )
    assert "fencing_epoch bigint NOT NULL DEFAULT 0" in sql
    assert "CREATE TABLE qc_device_lease" in sql
    assert "lease_token uuid NOT NULL" in sql
    assert "fencing_token bigint NOT NULL" in sql
    assert (
        "state IN ('claimed', 'active', 'releasing', 'recovering', 'released', 'quarantined')"
        in sql
    )
    assert "WHERE state IN ('claimed', 'active', 'releasing', 'recovering')" in sql
    assert "CREATE UNIQUE INDEX uq_qc_device_lease_active_session" in sql
    assert "ALTER TABLE qc_surface_session" in sql
    assert "ADD COLUMN device_lease_id uuid" in sql
    assert "ADD COLUMN device_lease_fencing_token bigint" in sql
    assert "fencing_token 用于拒绝旧 Worker 写入" in sql


def test_downgrade_refuses_to_delete_device_facts_before_drop() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    active_guard = sql.index("存在 App 设备租约事实")
    drop_table = sql.index("DROP TABLE IF EXISTS qc_device_lease")
    assert active_guard < drop_table
    assert "DROP COLUMN IF EXISTS device_lease_id" in sql
    assert "DROP COLUMN IF EXISTS device_lease_fencing_token" in sql
    assert "DROP INDEX IF EXISTS uq_qc_device_lease_active_device" in sql
