from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260907_0097_company_admin_role.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("company_admin_role_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_upgrade_allows_company_admin_role_and_names_headquarters() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260907_0097"
    assert migration.down_revision == "20260907_0096"
    assert "DROP CONSTRAINT IF EXISTS ck_qc_user_role" in sql
    assert "role IN ('平台管理员', '公司管理员', '审计员', '规则配置员')" in sql
    # 只在默认租户还叫迁移种子名时改名，运营手工改过的名字不动。
    assert "SET display_name = '体验中心'" in sql
    assert "AND display_name = '默认租户（历史数据归属）'" in sql
    assert migration.DEFAULT_TENANT_ID in sql


def test_downgrade_folds_company_admin_back_and_restores_constraint() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    statements = operations.statements
    # 先把公司管理员改回审计员，再收紧约束，否则约束会加不上。
    assert "SET role = '审计员'" in statements[0]
    assert "WHERE role = '公司管理员'" in statements[0]
    sql = " ".join(statements)
    assert "role IN ('平台管理员', '审计员', '规则配置员')" in sql
    assert "SET display_name = '默认租户（历史数据归属）'" in sql
