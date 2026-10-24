"""Web 正式报告生成 attempt 的持久审计契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.application.web_report_generation_audit import (
    AuditedWebFinalReportGenerator,
    WebReportGenerationAttempt,
)
from services.api.app.infrastructure.db.web_report_generation_attempt_repository import (
    PostgresWebReportGenerationAttemptRepository,
)
from services.api.app.worker import _final_report_generator_for_record


class _Generator:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def generate_task_report(self, task_id: str, **_kwargs) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"task": {"task_id": task_id}}


class _Ledger:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.finished: list[dict] = []

    async def start_attempt(self, **kwargs) -> WebReportGenerationAttempt:
        self.started.append(kwargs)
        return WebReportGenerationAttempt(
            attempt_id=kwargs["attempt_id"],
            task_id=kwargs["task_id"],
            agent_run_id=kwargs["agent_run_id"],
            finalization_epoch=7,
            attempt_number=len(self.started),
        )

    async def finish_attempt(self, **kwargs) -> None:
        self.finished.append(kwargs)


class _BrokenFinishLedger(_Ledger):
    async def finish_attempt(self, **kwargs) -> None:
        self.finished.append(kwargs)
        raise RuntimeError("audit unavailable")


class _RejectedStartLedger(_Ledger):
    async def start_attempt(self, **kwargs) -> WebReportGenerationAttempt:
        self.started.append(kwargs)
        raise RuntimeError("task is no longer reportable")


class _Acquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


class _Transaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.transaction_entered = True

    async def __aexit__(self, *_args) -> None:
        return None


class _Connection:
    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.calls = []
        self.transaction_entered = False

    def transaction(self):
        return _Transaction(self)

    async def fetchrow(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.rows.pop(0)


class _Pool:
    def __init__(self, rows) -> None:
        self.connection = _Connection(rows)

    def acquire(self):
        return _Acquire(self.connection)


async def test_audited_web_report_generator_records_each_formal_attempt() -> None:
    generator = _Generator()
    ledger = _Ledger()
    audited = AuditedWebFinalReportGenerator(delegate=generator, ledger=ledger)
    finalized_at = datetime(2026, 9, 3, tzinfo=UTC)

    await audited.generate_task_report(
        "task-1",
        agent_run_id="run-1",
        finalized_at=finalized_at,
    )
    await audited.generate_task_report(
        "task-1",
        agent_run_id="run-1",
        finalized_at=finalized_at,
    )

    assert generator.calls == 2
    assert [item["attempt_number"] for item in ledger.finished] == [1, 2]
    assert [item["status"] for item in ledger.finished] == ["succeeded", "succeeded"]
    assert all(item["failure_code"] is None for item in ledger.finished)


async def test_audited_web_report_generator_records_failure_before_reraising() -> None:
    generator = _Generator(error=RuntimeError("provider unavailable"))
    ledger = _Ledger()
    audited = AuditedWebFinalReportGenerator(delegate=generator, ledger=ledger)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await audited.generate_task_report(
            "task-1",
            agent_run_id="run-1",
            finalized_at=datetime(2026, 9, 3, tzinfo=UTC),
        )

    assert ledger.finished == [
        {
            "attempt_id": ledger.started[0]["attempt_id"],
            "attempt_number": 1,
            "status": "failed",
            "failure_code": "RuntimeError",
        }
    ]


async def test_audit_finish_failure_does_not_mask_report_generation_failure() -> None:
    generator = _Generator(error=ValueError("original generation failure"))
    ledger = _BrokenFinishLedger()
    audited = AuditedWebFinalReportGenerator(delegate=generator, ledger=ledger)

    with pytest.raises(ValueError, match="original generation failure"):
        await audited.generate_task_report(
            "task-1",
            agent_run_id="run-1",
            finalized_at=datetime(2026, 9, 3, tzinfo=UTC),
        )

    assert ledger.finished[0]["failure_code"] == "ValueError"


async def test_audited_web_report_generator_leaves_preview_unmodified() -> None:
    generator = _Generator()
    ledger = _Ledger()
    audited = AuditedWebFinalReportGenerator(delegate=generator, ledger=ledger)

    report = await audited.generate_task_report(
        "task-1",
        agent_run_id="run-1",
        finalized_at=None,
    )

    assert report == {"task": {"task_id": "task-1"}}
    assert ledger.started == []
    assert ledger.finished == []


async def test_audited_web_report_generator_does_not_call_delegate_when_start_rejected() -> None:
    generator = _Generator()
    ledger = _RejectedStartLedger()
    audited = AuditedWebFinalReportGenerator(delegate=generator, ledger=ledger)

    with pytest.raises(RuntimeError, match="no longer reportable"):
        await audited.generate_task_report(
            "task-1",
            agent_run_id="run-1",
            finalized_at=datetime(2026, 9, 3, tzinfo=UTC),
        )

    assert len(ledger.started) == 1
    assert generator.calls == 0
    assert ledger.finished == []


async def test_postgres_attempt_ledger_serializes_sequence_and_finishes_idempotently() -> None:
    pool = _Pool(
        [
            {"task_id": "task-1", "finalization_epoch": 7},
            {
                "attempt_id": "attempt-1",
                "task_id": "task-1",
                "agent_run_id": "run-1",
                "finalization_epoch": 7,
                "attempt_number": 3,
            },
            {"attempt_id": "attempt-1"},
        ]
    )
    ledger = PostgresWebReportGenerationAttemptRepository(pool=pool)

    attempt = await ledger.start_attempt(
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="run-1",
    )
    await ledger.finish_attempt(
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        status="succeeded",
        failure_code=None,
    )

    task_query, task_args = pool.connection.calls[0]
    insert_query, insert_args = pool.connection.calls[1]
    finish_query, finish_args = pool.connection.calls[2]
    assert pool.connection.transaction_entered is True
    assert "FOR UPDATE" in task_query
    assert "task.target_platform = 'web'" in task_query
    assert "run.agent_type = 'main'" in task_query
    assert "task.status IN ('running', 'review')" in task_query
    assert "task.finalization_state = 'session_draining'" in task_query
    assert "task.finalization_agent_run_id = run.id" in task_query
    assert "run.status IN ('done', 'failed', 'aborted')" in task_query
    assert "task.completion_gate_snapshot_id IS NULL" not in task_query
    assert "task.finalization_last_error IS NULL" not in task_query
    assert "web_formal_terminalized" in task_query
    assert "cancel_requested" in task_query
    assert "FROM qc_web_coverage_snapshot AS coverage" in task_query
    assert "coverage.finalization_epoch = task.finalization_epoch" in task_query
    assert task_args == ("task-1", "run-1")
    assert "COALESCE(MAX(existing.attempt_number), 0) + 1" in insert_query
    assert "failure_code = 'worker_interrupted'" in insert_query
    assert "ON CONFLICT (id) DO NOTHING" in insert_query
    assert insert_args == ("attempt-1", "task-1", "run-1", 7)
    assert "status = 'running'" in finish_query
    assert "existing.status = $3" in finish_query
    assert finish_args == ("attempt-1", 3, "succeeded", None)


def test_audited_generator_is_selected_only_for_locked_web_full_run() -> None:
    default = object()
    audited = object()
    common = {
        "workflow_version": "v2",
        "web_inspection_notification_mode": "enforced",
    }

    selected = _final_report_generator_for_record(
        SimpleNamespace(
            task_config={
                **common,
                "_task_target": {"platform": "web"},
                "execution_mode": "full_inspection",
            }
        ),
        default_generator=default,
        web_generator=audited,
    )
    exploration = _final_report_generator_for_record(
        SimpleNamespace(
            task_config={
                **common,
                "_task_target": {"platform": "web"},
                "execution_mode": "exploration_only",
            }
        ),
        default_generator=default,
        web_generator=audited,
    )
    h5 = _final_report_generator_for_record(
        SimpleNamespace(
            task_config={
                **common,
                "_task_target": {"platform": "h5"},
                "execution_mode": "full_inspection",
            }
        ),
        default_generator=default,
        web_generator=audited,
    )

    assert selected is audited
    assert exploration is default
    assert h5 is default
