"""0054 App 探索运行时检查点迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0055_app_exploration_runtime_checkpoint.py"
)


class RecordingOperations:
    """只记录迁移 SQL，避免合同测试依赖真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_exploration_runtime_checkpoint_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_app_only_runtime_checkpoint_contract() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260814_0055"
    assert migration.down_revision == "20260814_0054"
    assert "CREATE TABLE qc_app_exploration_runtime_checkpoint" in sql
    assert "agent_run_id uuid PRIMARY KEY" in sql
    assert "REFERENCES qc_agent_run(id) ON DELETE CASCADE" in sql
    assert "task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE" in sql
    assert "surface_session_id varchar(128) NOT NULL" in sql
    assert "platform_build_ref varchar(255) NOT NULL" in sql
    assert "bootstrap_capture_id uuid NOT NULL" in sql
    assert "REFERENCES qc_capture(id) ON DELETE RESTRICT" in sql
    assert "installed_package varchar(255) NOT NULL" in sql
    assert "protocol_version varchar(128) NOT NULL" in sql
    assert "runtime_policy jsonb NOT NULL" in sql
    assert "runtime_policy_sha256 varchar(128) NOT NULL" in sql
    assert "objective_hash varchar(128) NOT NULL" in sql
    assert "started_at timestamptz NOT NULL" in sql
    assert "deadline timestamptz NOT NULL" in sql
    assert "created_at timestamptz NOT NULL DEFAULT now()" in sql
    assert "updated_at timestamptz NOT NULL DEFAULT now()" in sql
    assert "jsonb_typeof(runtime_policy) = 'object'" in sql
    assert "runtime_policy_sha256 ~ '^sha256:[0-9a-f]{64}$'" in sql
    assert "objective_hash ~ '^[0-9a-f]{64}$'" in sql
    assert "deadline > started_at" in sql
    assert "FOREIGN KEY (task_id, surface_session_id)" in sql
    assert "CREATE OR REPLACE FUNCTION qc_guard_app_exploration_runtime_checkpoint()" in sql
    assert "run.task_id = NEW.task_id" in sql
    assert "task.target_platform = 'app'" in sql
    assert "session.surface_kind IN ('android', 'ios')" in sql
    assert "session.status = 'active'" in sql
    assert "session.platform_build_ref = NEW.platform_build_ref" in sql
    assert "capture.agent_run_id = run.id" in sql
    assert "capture.capture_status = 'captured'" in sql
    assert "capture.capture_role = 'bootstrap'" in sql
    assert "bootstrap.stage = 'app_ready'" in sql
    assert "bootstrap.bootstrap_capture_id = NEW.bootstrap_capture_id" in sql
    assert "bootstrap.installed_package = NEW.installed_package" in sql
    assert "身份、策略、目标和时间窗口禁止修改" in sql
    assert "trg_qc_app_exploration_runtime_checkpoint_guard" in sql
    assert "TG_OP = 'DELETE'" in sql
    assert "pg_trigger_depth() <= 1" in sql
    assert "禁止直接删除" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE ON qc_app_exploration_runtime_checkpoint" in sql


def test_downgrade_checks_any_checkpoint_row_before_drop() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    guard_position = sql.index("存在 App 探索运行时检查点事实")
    drop_position = sql.index("DROP TABLE IF EXISTS qc_app_exploration_runtime_checkpoint")
    assert guard_position < drop_position
    assert "IF EXISTS ( SELECT 1 FROM qc_app_exploration_runtime_checkpoint )" in sql
    assert "DROP TRIGGER IF EXISTS trg_qc_app_exploration_runtime_checkpoint_guard" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_app_exploration_runtime_checkpoint()" in sql
