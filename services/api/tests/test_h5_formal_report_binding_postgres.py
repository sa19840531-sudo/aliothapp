"""H5 V2 正式报告读取、门禁封存与报告正文绑定的真实 PostgreSQL 回归。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from services.api.app import worker
from services.api.app.application.report_payloads import stable_report_payload_hash
from services.api.app.application.task_finalization import (
    BindReportToGateCommand,
    FinalizeTaskWithReportCommand,
)
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord
from services.api.tests.test_h5_report_coverage import coverage_payload
from services.api.tests.test_review_finalization_recovery_postgres import (
    _gate_command,
    _PostgresHarness,
)
from services.api.tests.test_review_finalization_recovery_postgres import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_review_finalization_recovery_postgres import (
    pg as pg,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


@pytest.fixture
async def h5_pg(
    pg: _PostgresHarness,
) -> AsyncIterator[_PostgresHarness]:
    """在既有 F01 隔离库上运行，并清理本文件生成的 H5 快照。"""

    try:
        yield pg
    finally:
        task_ids = [case.task_id for case in pg.cases]
        if task_ids:
            async with pg.connection.transaction():
                await pg.connection.execute(
                    "SET LOCAL session_replication_role = 'replica'"
                )
                await pg.connection.execute(
                    """
                    DELETE FROM qc_h5_coverage_snapshot
                    WHERE task_id = ANY($1::uuid[])
                    """,
                    task_ids,
                )
                await pg.connection.execute(
                    """
                    DELETE FROM qc_surface_session
                    WHERE task_id = ANY($1::uuid[])
                    """,
                    task_ids,
                )


async def _prepare_formal_h5_case(pg: _PostgresHarness):
    """把 F01 最小任务图切换为 H5 V2 full_inspection 合同。"""

    case = await pg.seed_case(
        task_status="running",
        session_draining=True,
        include_completed_exploration=True,
    )
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET target_platform = 'h5',
            task_config = $2::jsonb
        WHERE id = $1::uuid
        """,
        case.task_id,
        json.dumps(
            {
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
            }
        ),
    )
    session_id = f"f01-session-{case.task_id}"
    await pg.connection.execute(
        """
        INSERT INTO qc_surface_session(
            session_id, task_id, surface_kind, session_owner, status,
            finalization_epoch, activated_at, draining_at
        )
        VALUES(
            $1, $2::uuid, 'web', 'h5-formal-report-pg', 'draining',
            0, now(), now()
        )
        """,
        session_id,
        case.task_id,
    )
    await pg.connection.execute(
        """
        UPDATE qc_v2_work_item
        SET work_payload = $3::jsonb
        WHERE task_id = $1::uuid
          AND session_id = $2
          AND work_type = 'exploration'
        """,
        case.task_id,
        session_id,
        json.dumps(
            {
                "frontier": {
                    "observation": {
                        "metadata": {"agent_run_id": case.run_id}
                    }
                }
            }
        ),
    )
    await pg.connection.execute(
        """
        UPDATE qc_page_capture
        SET agent_run_id = $2::uuid,
            surface_session_id = $3,
            capture_role = 'after_action',
            session_command_sequence = 1
        WHERE id = $1::uuid
        """,
        case.capture_id,
        case.run_id,
        session_id,
    )
    return case


def _coverage_for(case: Any) -> dict[str, Any]:
    """使用领域 helper 产生可由 H5CoverageSnapshot 校验的内容哈希。"""

    coverage = coverage_payload(
        task_id=case.task_id,
        agent_run_id=case.run_id,
        epoch=0,
    )
    coverage["coverage_snapshot_id"] = f"h5-formal-{uuid4()}"
    return coverage


def _coverage_hash(coverage: dict[str, Any]) -> str:
    return stable_protocol_hash(
        {
            key: value
            for key, value in coverage.items()
            if key
            not in {
                "coverage_snapshot_id",
                "task_id",
                "agent_run_id",
                "finalization_epoch",
                "payload_hash",
            }
        }
    )


async def _insert_domain_coverage(connection: Any, coverage: dict[str, Any]) -> None:
    """把领域 helper 的完整 Coverage 投影写入不可变事实表。"""

    await connection.execute(
        """
        INSERT INTO qc_h5_coverage_snapshot(
            coverage_snapshot_id, task_id, agent_run_id, finalization_epoch,
            revision, authority_source, required_page_types, covered_page_types,
            missing_page_types, coverage_scopes, assessment_scope_ready,
            frontier_terminal, completion_ready, schema_version, payload_hash
        )
        VALUES(
            $1, $2::uuid, $3::uuid, $4, $5, $6, $7::jsonb, $8::jsonb,
            $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15
        )
        """,
        coverage["coverage_snapshot_id"],
        coverage["task_id"],
        coverage["agent_run_id"],
        coverage["finalization_epoch"],
        coverage["revision"],
        coverage["authority_source"],
        json.dumps(coverage["required_page_types"]),
        json.dumps(coverage["covered_page_types"]),
        json.dumps(coverage["missing_page_types"]),
        json.dumps(coverage["scopes"], ensure_ascii=False),
        coverage["assessment_scope_ready"],
        coverage["frontier_terminal"],
        coverage["completion_ready"],
        coverage["schema_version"],
        coverage["payload_hash"],
    )


def _late_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    late = deepcopy(coverage)
    late["coverage_snapshot_id"] = f"h5-formal-late-{uuid4()}"
    late["revision"] = int(coverage["revision"]) + 1
    late["payload_hash"] = _coverage_hash(late)
    return late


class _DatabasePageReader:
    """同时提供 Worker 所需的 Coverage 读取和完整页面时间线读取。"""

    def __init__(self, pool: Any) -> None:
        repository = PostgresTaskInsightRepository(pool=pool)
        self._coverage_repository = repository
        self._insight_service = TaskInsightService(repository=repository)

    async def fetch_h5_coverage_snapshot(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any] | None:
        return await self._coverage_repository.fetch_h5_coverage_snapshot(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def page_timeline(self, task_id: str) -> dict[str, Any]:
        return await self._insight_service.page_timeline(task_id)


def _worker_record(case: Any, *, run_id: str | None = None) -> AgentRunRecord:
    return AgentRunRecord(
        run_id=run_id or case.run_id,
        task_id=case.task_id,
        message="H5 V2 正式报告回归",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "_task_target": {"platform": "h5", "target_url": "https://f01.example.test/"},
        },
    )


async def _worker_page_exploration(
    pg: _PostgresHarness,
    case: Any,
    *,
    run_id: str | None = None,
) -> tuple[AgentRunRecord, dict[str, Any]]:
    record = _worker_record(case, run_id=run_id)
    pages = await worker._page_exploration_for_report(
        record,
        reader=_DatabasePageReader(pg.pool),
    )
    return record, pages


def _report(page_exploration: dict[str, Any]) -> dict[str, Any]:
    coverage = page_exploration["h5_coverage"]
    report = {
        "task": {"task_id": coverage["task_id"]},
        "agent_run_id": coverage["agent_run_id"],
        "report_version": "v2",
        "summary": {"overall_status": "complete"},
        "page_exploration": page_exploration,
    }
    gate_snapshot_id = coverage.get("gate_snapshot_id")
    gate_finalization_epoch = coverage.get("gate_finalization_epoch")
    if gate_snapshot_id is not None and gate_finalization_epoch is not None:
        report.update(
            {
                "completion_gate": {
                    "gate_passed": True,
                    "gate_snapshot_id": gate_snapshot_id,
                    "finalization_epoch": gate_finalization_epoch,
                },
                "report_stage": "final",
                "is_final": True,
            }
        )
    return report


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    assert isinstance(value, dict)
    return value


def _report_command(
    case: Any,
    row: Any,
    report: dict[str, Any],
) -> FinalizeTaskWithReportCommand:
    return FinalizeTaskWithReportCommand(
        task_id=case.task_id,
        agent_run_id=case.run_id,
        report_id=f"task:{case.task_id}:run:{case.run_id}",
        report_version=2,
        expected_finalization_epoch=int(row["finalization_epoch"]),
        expected_row_version=int(row["row_version"]),
        execution_status=TaskExecutionStatus.COMPLETED,
        quality_outcome=TaskQualityOutcome.UNKNOWN,
        final_report=report,
    )


def _bind_command(
    case: Any,
    report: dict[str, Any],
    *,
    gate_snapshot_id: str,
    finalization_epoch: int,
) -> BindReportToGateCommand:
    return BindReportToGateCommand(
        report_id=str(report["report_id"]),
        report_version=int(report["report_binding_version"]),
        report_payload_hash=stable_report_payload_hash(report),
        final_report=report,
        task_id=case.task_id,
        gate_snapshot_id=gate_snapshot_id,
        finalization_epoch=finalization_epoch,
        agent_run_id=case.run_id,
    )


async def _artifact_counts(pg: _PostgresHarness, task_id: str) -> tuple[int, int, int]:
    row = await pg.connection.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM qc_completion_gate_snapshot
             WHERE task_id = $1::uuid),
            (SELECT count(*) FROM qc_report_gate_binding
             WHERE task_id = $1::uuid),
            (SELECT count(*)
             FROM qc_report_payload_snapshot AS payload
             INNER JOIN qc_report_gate_binding AS binding
               ON binding.report_id = payload.report_id
              AND binding.report_version = payload.report_version
             WHERE binding.task_id = $1::uuid)
        """,
        task_id,
    )
    assert row is not None
    return tuple(int(value) for value in row)


def _task_identity(row: Any) -> tuple[Any, ...]:
    return tuple(
        row[key]
        for key in (
            "status",
            "finalization_state",
            "finalization_epoch",
            "row_version",
            "completion_gate_snapshot_id",
        )
    )


@pytest.mark.asyncio
async def test_worker_h5_report_finalization_persists_domain_coverage_identity(
    h5_pg: _PostgresHarness,
) -> None:
    """Worker 读取的实际 Coverage 必须和 Gate 及报告正文保持同一身份。"""

    case = await _prepare_formal_h5_case(h5_pg)
    coverage = _coverage_for(case)
    await _insert_domain_coverage(h5_pg.connection, coverage)
    _record, page_exploration = await _worker_page_exploration(h5_pg, case)
    frozen = page_exploration["h5_coverage"]
    assert frozen["coverage_snapshot_id"] == coverage["coverage_snapshot_id"]
    assert frozen["payload_hash"] == coverage["payload_hash"]
    assert frozen["finalization_epoch"] == 0

    report = _report(page_exploration)
    repository = PostgresTaskFinalizationRepository(pool=h5_pg.pool)
    decision = await repository.finalize_task_with_report(
        command=_report_command(case, await h5_pg.task_row(case.task_id), report)
    )
    assert decision.snapshot.gate_passed is True
    assert decision.snapshot.finalization_epoch == 1
    assert decision.snapshot.facts.h5_coverage_snapshot_id == coverage["coverage_snapshot_id"]
    assert decision.snapshot.facts.h5_coverage_snapshot_hash == coverage["payload_hash"]
    assert decision.snapshot.facts.h5_coverage_snapshot_epoch == 0

    stored = await h5_pg.connection.fetchrow(
        """
        SELECT gate.h5_coverage_snapshot_id,
               gate.h5_coverage_snapshot_hash,
               gate.h5_coverage_snapshot_epoch,
               payload.report_payload,
               run.output_context->'final_report' AS run_report
        FROM qc_task AS task
        INNER JOIN qc_completion_gate_snapshot AS gate
          ON gate.gate_snapshot_id = task.completion_gate_snapshot_id
         AND gate.task_id = task.id
         AND gate.finalization_epoch = task.finalization_epoch
        INNER JOIN qc_report_gate_binding AS binding
          ON binding.task_id = task.id
         AND binding.gate_snapshot_id = gate.gate_snapshot_id
         AND binding.finalization_epoch = gate.finalization_epoch
        INNER JOIN qc_report_payload_snapshot AS payload
          ON payload.report_id = binding.report_id
         AND payload.report_version = binding.report_version
        INNER JOIN qc_agent_run AS run
          ON run.id = task.finalization_agent_run_id
        WHERE task.id = $1::uuid
        """,
        case.task_id,
    )
    assert stored is not None
    assert tuple(stored[:3]) == (
        coverage["coverage_snapshot_id"],
        coverage["payload_hash"],
        0,
    )
    for body in (
        _json_object(stored["report_payload"]),
        _json_object(stored["run_report"]),
    ):
        assert body["page_exploration"]["h5_coverage"]["coverage_snapshot_id"] == (
            coverage["coverage_snapshot_id"]
        )
        assert body["page_exploration"]["h5_coverage"]["payload_hash"] == coverage[
            "payload_hash"
        ]
        assert body["page_exploration"]["h5_coverage"]["finalization_epoch"] == 0
        assert body["page_exploration"]["h5_coverage"]["gate_snapshot_id"] == (
            decision.snapshot.gate_snapshot_id
        )
        assert body["page_exploration"]["h5_coverage"]["gate_finalization_epoch"] == 1
        assert body["completion_gate"]["finalization_epoch"] == 1


@pytest.mark.asyncio
async def test_h5_late_revision_rejects_finalization_without_half_written_report(
    h5_pg: _PostgresHarness,
) -> None:
    """报告生成后出现更高修订时，封存必须整体回滚。"""

    case = await _prepare_formal_h5_case(h5_pg)
    coverage = _coverage_for(case)
    await _insert_domain_coverage(h5_pg.connection, coverage)
    _record, page_exploration = await _worker_page_exploration(h5_pg, case)
    report = _report(page_exploration)
    before_task = await h5_pg.task_row(case.task_id)
    before_run = await h5_pg.connection.fetchval(
        "SELECT output_context FROM qc_agent_run WHERE id = $1::uuid",
        case.run_id,
    )
    before_counts = await _artifact_counts(h5_pg, case.task_id)

    await _insert_domain_coverage(h5_pg.connection, _late_coverage(coverage))
    repository = PostgresTaskFinalizationRepository(pool=h5_pg.pool)
    with pytest.raises(TaskFinalizationPersistenceError, match="H5 正式报告绑定校验失败"):
        await repository.finalize_task_with_report(
            command=_report_command(case, before_task, report)
        )

    after_task = await h5_pg.task_row(case.task_id)
    after_run = await h5_pg.connection.fetchval(
        "SELECT output_context FROM qc_agent_run WHERE id = $1::uuid",
        case.run_id,
    )
    assert _task_identity(after_task) == _task_identity(before_task)
    assert after_run == before_run
    assert await _artifact_counts(h5_pg, case.task_id) == before_counts == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_run", [False, True])
async def test_worker_h5_report_rejects_missing_or_cross_run_coverage(
    h5_pg: _PostgresHarness,
    cross_run: bool,
) -> None:
    """缺少当前快照或把另一个 Run 的快照投影过来都必须拒绝。"""

    case = await _prepare_formal_h5_case(h5_pg)
    run_id = case.run_id
    if cross_run:
        other = await _prepare_formal_h5_case(h5_pg)
        await _insert_domain_coverage(h5_pg.connection, _coverage_for(other))
        run_id = other.run_id

    with pytest.raises(RuntimeError, match="Coverage Snapshot"):
        await _worker_page_exploration(h5_pg, case, run_id=run_id)


@pytest.mark.asyncio
async def test_h5_bind_report_rechecks_gate_identity_and_keeps_old_revision(
    h5_pg: _PostgresHarness,
) -> None:
    """独立 bind_report 入口复用 Gate 身份，晚写与篡改正文均不得替换。"""

    case = await _prepare_formal_h5_case(h5_pg)
    coverage = _coverage_for(case)
    await _insert_domain_coverage(h5_pg.connection, coverage)
    repository = PostgresTaskFinalizationRepository(pool=h5_pg.pool)
    decision = await repository.evaluate_and_seal(
        command=_gate_command(
            case,
            await h5_pg.task_row(case.task_id),
            execution_status=TaskExecutionStatus.COMPLETED,
        )
    )
    assert decision.snapshot.gate_passed is True
    assert decision.snapshot.finalization_epoch == 1

    record, page_exploration = await _worker_page_exploration(h5_pg, case)
    record.final_report = _report(page_exploration)
    worker._prepare_report_binding_metadata(record)
    worker._attach_completion_gate(
        record,
        type(
            "Decision",
            (),
            {
                "gate_passed": True,
                "gate_snapshot_id": decision.snapshot.gate_snapshot_id,
                "finalization_epoch": decision.snapshot.finalization_epoch,
                "block_reasons": (),
            },
        )(),
    )
    assert record.final_report is not None
    report = record.final_report
    binding = await repository.bind_report(
        command=_bind_command(
            case,
            report,
            gate_snapshot_id=decision.snapshot.gate_snapshot_id,
            finalization_epoch=decision.snapshot.finalization_epoch,
        )
    )
    assert binding.gate_snapshot_id == decision.snapshot.gate_snapshot_id
    assert binding.finalization_epoch == 1

    await _insert_domain_coverage(h5_pg.connection, _late_coverage(coverage))
    read_back = await PostgresTaskInsightRepository(
        pool=h5_pg.pool
    ).fetch_h5_coverage_snapshot(task_id=case.task_id, agent_run_id=case.run_id)
    assert read_back is not None
    assert read_back["coverage_snapshot_id"] == coverage["coverage_snapshot_id"]
    assert read_back["payload_hash"] == coverage["payload_hash"]
    assert read_back["revision"] == coverage["revision"]

    root_gate_tampered = deepcopy(report)
    root_gate_tampered["completion_gate"]["gate_snapshot_id"] = (
        f"wrong-{uuid4()}"
    )
    with pytest.raises(TaskFinalizationPersistenceError, match="H5 正式报告绑定校验失败"):
        await repository.bind_report(
            command=_bind_command(
                case,
                root_gate_tampered,
                gate_snapshot_id=decision.snapshot.gate_snapshot_id,
                finalization_epoch=decision.snapshot.finalization_epoch,
            )
        )

    tampered = deepcopy(report)
    tampered["page_exploration"]["h5_coverage"]["payload_hash"] = "0" * 64
    with pytest.raises(TaskFinalizationPersistenceError, match="H5 正式报告绑定校验失败"):
        await repository.bind_report(
            command=_bind_command(
                case,
                tampered,
                gate_snapshot_id=decision.snapshot.gate_snapshot_id,
                finalization_epoch=decision.snapshot.finalization_epoch,
            )
        )
    assert await _artifact_counts(h5_pg, case.task_id) == (1, 1, 1)
