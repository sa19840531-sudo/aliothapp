"""根任务状态与跨工作类型生命周期投影测试。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.inspection_work_items import InspectionWorkItemStatus
from services.api.app.application.task_status import TransitionTaskStatusCommand
from services.api.app.application.work_statuses import (
    WorkLifecyclePhase,
    WorkTerminalOutcome,
    WorkType,
    project_work_status,
)
from services.api.app.domain.task.status import (
    TASK_STATUS_TRANSITIONS,
    TaskStatus,
    TaskStatusConflictError,
    TaskStatusValidationError,
    validate_task_status_transition,
)
from services.api.app.infrastructure.db.task_status_repository import (
    transition_task_status_in_transaction,
)

_OCCURRED_AT = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260805_0034_unified_task_status.py"
)


class _TaskConnection:
    def __init__(
        self,
        *,
        status: TaskStatus = TaskStatus.QUEUED,
        row_version: int = 0,
        finalization_state: str = "open",
    ) -> None:
        self.status = status
        self.row_version = row_version
        self.finalization_state = finalization_state
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if compact.startswith("SELECT"):
            return self._snapshot(task_id=str(args[0]))
        if compact.startswith("UPDATE qc_task"):
            if (
                self.status.value != args[1]
                or self.row_version != args[2]
                or self.finalization_state == "sealed"
            ):
                return None
            self.status = TaskStatus(str(args[3]))
            self.row_version += 1
            return self._snapshot(task_id=str(args[0]))
        raise AssertionError(f"未预期的查询：{compact}")

    def _snapshot(self, *, task_id: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": self.status.value,
            "row_version": self.row_version,
            "finalization_state": self.finalization_state,
        }


def _command(
    *,
    expected_status: TaskStatus,
    target_status: TaskStatus,
    expected_row_version: int,
) -> TransitionTaskStatusCommand:
    return TransitionTaskStatusCommand(
        task_id="task-1",
        expected_status=expected_status,
        target_status=target_status,
        expected_row_version=expected_row_version,
        occurred_at=_OCCURRED_AT,
    )


def test_task_status_transition_matrix_rejects_illegal_moves() -> None:
    for current_status in TaskStatus:
        for target_status in TaskStatus:
            if current_status is target_status:
                validate_task_status_transition(
                    current_status=current_status,
                    target_status=target_status,
                    finalization_state="open",
                )
                continue
            if target_status in TASK_STATUS_TRANSITIONS[current_status]:
                validate_task_status_transition(
                    current_status=current_status,
                    target_status=target_status,
                    finalization_state="open",
                )
                continue
            with pytest.raises(TaskStatusValidationError, match="非法任务状态转换"):
                validate_task_status_transition(
                    current_status=current_status,
                    target_status=target_status,
                    finalization_state="open",
                )


def test_task_status_rejects_sealed_writeback_but_accepts_same_state_replay() -> None:
    validate_task_status_transition(
        current_status=TaskStatus.DONE,
        target_status=TaskStatus.DONE,
        finalization_state="sealed",
    )

    with pytest.raises(TaskStatusConflictError, match="sealed"):
        validate_task_status_transition(
            current_status=TaskStatus.DONE,
            target_status=TaskStatus.REVIEW,
            finalization_state="sealed",
        )


async def test_task_state_replay_keeps_row_version() -> None:
    connection = _TaskConnection(status=TaskStatus.RUNNING, row_version=7)

    snapshot = await transition_task_status_in_transaction(
        connection,
        command=_command(
            expected_status=TaskStatus.RUNNING,
            target_status=TaskStatus.RUNNING,
            expected_row_version=7,
        ),
        allowed_source_statuses=(TaskStatus.RUNNING,),
    )

    assert snapshot is not None
    assert snapshot.row_version == 7
    assert len(connection.calls) == 1
    assert connection.calls[0][0].endswith("FOR UPDATE")


async def test_task_state_rejects_stale_version() -> None:
    connection = _TaskConnection(status=TaskStatus.RUNNING, row_version=4)

    snapshot = await transition_task_status_in_transaction(
        connection,
        command=_command(
            expected_status=TaskStatus.RUNNING,
            target_status=TaskStatus.DONE,
            expected_row_version=3,
        ),
        allowed_source_statuses=(TaskStatus.RUNNING,),
    )

    assert snapshot is None
    assert len(connection.calls) == 1


async def test_task_state_update_uses_status_version_and_sealed_guards() -> None:
    connection = _TaskConnection(status=TaskStatus.RUNNING, row_version=4)

    snapshot = await transition_task_status_in_transaction(
        connection,
        command=_command(
            expected_status=TaskStatus.RUNNING,
            target_status=TaskStatus.REVIEW,
            expected_row_version=4,
        ),
        allowed_source_statuses=(TaskStatus.RUNNING,),
    )

    assert snapshot is not None
    assert snapshot.status is TaskStatus.REVIEW
    assert snapshot.row_version == 5
    update_query, update_args = connection.calls[1]
    assert "status = $2" in update_query
    assert "row_version = $3" in update_query
    assert "finalization_state <> 'sealed'" in update_query
    assert update_args[:5] == (
        "task-1",
        "running",
        4,
        "review",
        _OCCURRED_AT,
    )


def test_all_v2_work_statuses_have_normalized_lifecycle_projection() -> None:
    status_sets = {
        WorkType.EXPLORATION: ExplorerWorkStatus,
        WorkType.EVIDENCE: EvidenceRequestStatus,
        WorkType.INSPECTION: InspectionWorkItemStatus,
    }

    for work_type, status_enum in status_sets.items():
        projections = {
            project_work_status(work_type=work_type, raw_status=status.value).raw_status
            for status in status_enum
        }
        assert projections == {status.value for status in status_enum}


def test_terminal_projection_preserves_domain_outcome() -> None:
    cases = {
        (WorkType.EXPLORATION, "succeeded"): WorkTerminalOutcome.SUCCEEDED,
        (WorkType.EXPLORATION, "budget_exhausted"): WorkTerminalOutcome.BOUNDED,
        (WorkType.EVIDENCE, "unfulfillable"): WorkTerminalOutcome.UNFULFILLABLE,
        (WorkType.INSPECTION, "partial"): WorkTerminalOutcome.PARTIAL,
        (WorkType.INSPECTION, "failed"): WorkTerminalOutcome.FAILED,
    }

    for (work_type, raw_status), expected_outcome in cases.items():
        projection = project_work_status(work_type=work_type, raw_status=raw_status)
        assert projection.phase is WorkLifecyclePhase.TERMINAL
        assert projection.terminal_outcome is expected_outcome


def test_task_status_migration_guards_all_legacy_write_paths() -> None:
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "20260805_0034"' in migration
    assert 'down_revision = "20260804_0037"' in migration
    assert "ADD COLUMN row_version bigint NOT NULL DEFAULT 0" in migration
    assert "CREATE TRIGGER trg_qc_task_status_transition" in migration
    assert "OLD.finalization_state = 'sealed'" in migration
    assert "NEW.row_version := OLD.row_version + 1" in migration
    assert "NEW.row_version := OLD.row_version" in migration
    for source, targets in TASK_STATUS_TRANSITIONS.items():
        if not targets:
            continue
        assert f"OLD.status = '{source.value}'" in migration
