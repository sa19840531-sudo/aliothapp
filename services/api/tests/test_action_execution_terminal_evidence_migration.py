"""0052 动作终态 After 证据迁移合同测试。"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from services.api.app.domain.interaction import (
    ActionExecutionSnapshot,
    ActionExecutionStatus,
)

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0053_action_execution_terminal_evidence.py"
)


class RecordingOperations:
    """只记录 SQL，避免合同测试依赖真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "action_execution_terminal_evidence_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_replaces_action_state_check_after_history_validation() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260814_0053"
    assert migration.down_revision == "20260814_0052"
    assert sql.index("存在不符合 0052 合同的动作终态证据") < sql.index(
        "DROP CONSTRAINT IF EXISTS ck_qc_action_execution_state"
    )
    assert "status IN ('blocked', 'failed', 'timed_out')" in sql
    assert "result_capture_id IS NOT NULL" in sql
    assert "evidence_finished_at IS NOT NULL" in sql
    assert "result.surface_kind IS DISTINCT FROM" in sql
    assert "result.capture_role IS DISTINCT FROM 'after_action'" in sql
    assert "result.capture_status IS DISTINCT FROM 'captured'" in sql
    assert "result.parent_capture_id IS DISTINCT FROM action.source_capture_id" in sql
    assert "ADD CONSTRAINT ck_qc_action_execution_state CHECK" in sql


def test_downgrade_refuses_terminal_evidence_before_restoring_legacy_check() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    guard_position = sql.index("terminal-with-evidence")
    drop_position = sql.index("DROP CONSTRAINT IF EXISTS ck_qc_action_execution_state")
    assert guard_position < drop_position
    legacy_check = sql[sql.rindex("ADD CONSTRAINT ck_qc_action_execution_state") :]
    assert "result_capture_id IS NULL" in legacy_check
    assert "evidence_finished_at IS NULL" in legacy_check
    assert "status IN ('blocked', 'failed', 'timed_out')" not in legacy_check
    assert "RAISE EXCEPTION" in sql


@pytest.mark.parametrize(
    "status",
    (
        ActionExecutionStatus.BLOCKED,
        ActionExecutionStatus.FAILED,
        ActionExecutionStatus.TIMED_OUT,
    ),
)
def test_terminal_snapshot_accepts_bound_after_evidence(
    status: ActionExecutionStatus,
) -> None:
    finished_at = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    snapshot = ActionExecutionSnapshot(
        action_execution_id="action-execution:terminal-evidence",
        task_id="00000000-0000-0000-0000-000000000001",
        session_id="app-session-1",
        intent_id="intent-1",
        intent_hash="hash-1",
        command_sequence=1,
        finalization_epoch=0,
        requested_source_capture_id="11111111-1111-1111-1111-111111111111",
        source_capture_id="11111111-1111-1111-1111-111111111111",
        result_capture_id="22222222-2222-2222-2222-222222222222",
        status=status,
        row_version=3,
        result_payload={"status": status.value},
        error_code="APP_ACTION_FAILED",
        started_at=finished_at,
        action_finished_at=finished_at,
        evidence_finished_at=finished_at,
    )

    assert snapshot.status is status
    assert snapshot.result_capture_id is not None
    assert snapshot.evidence_finished_at is finished_at
