"""H5 Coverage 与 Completion Gate 封存身份的真实 PostgreSQL 回归。"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest

from services.api.app.domain.task import TaskExecutionStatus
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
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


class _PinnedAcquire:
    """让读仓储在同一连接事务中观察故障注入后的快照身份。"""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False


class _PinnedPool:
    """只提供读仓储所需的连接获取接口。"""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def acquire(self) -> _PinnedAcquire:
        return _PinnedAcquire(self._connection)


async def _prepare_h5_case(pg: _PostgresHarness):
    """构造进入 Session Draining 的 H5 任务，并保留已完成探索 Work。"""

    case = await pg.seed_case(
        task_status="running",
        session_draining=True,
        include_completed_exploration=True,
    )
    await pg.connection.execute(
        "UPDATE qc_task SET target_platform = 'h5' WHERE id = $1::uuid",
        case.task_id,
    )
    return case


async def _insert_h5_snapshot(
    connection: Any,
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
    revision: int,
    coverage_snapshot_id: str,
    payload_hash: str,
    completion_ready: bool = True,
) -> None:
    """写入最小完整 Coverage 快照，模拟探索端的不可变事实。"""

    await connection.execute(
        """
        INSERT INTO qc_h5_coverage_snapshot(
            coverage_snapshot_id, task_id, agent_run_id, finalization_epoch,
            revision, authority_source, required_page_types, covered_page_types,
            missing_page_types, coverage_scopes, assessment_scope_ready,
            frontier_terminal, completion_ready, schema_version, payload_hash
        )
        VALUES(
            $1, $2::uuid, $3::uuid, $4, $5, 'h5_page_type_assessment',
            '[\"home\"]'::jsonb, '[\"home\"]'::jsonb, '[]'::jsonb,
            '[{\"page_type\":\"home\",\"assessment_status\":\"covered\"}]'::jsonb,
            true, true, $6, 'h5_coverage.v1', $7
        )
        """,
        coverage_snapshot_id,
        task_id,
        agent_run_id,
        finalization_epoch,
        revision,
        completion_ready,
        payload_hash,
    )


@pytest.mark.asyncio
async def test_h5_gate_persists_consumed_coverage_and_insight_ignores_late_revision(
    pg: _PostgresHarness,
) -> None:
    """Gate 封存后即使任务 epoch 前进，读侧仍使用 Gate 消费的 Coverage。"""

    case = await _prepare_h5_case(pg)
    consumed_id = f"h5-consumed-{uuid4()}"
    consumed_hash = "a" * 64
    await _insert_h5_snapshot(
        pg.connection,
        task_id=case.task_id,
        agent_run_id=case.run_id,
        finalization_epoch=0,
        revision=1,
        coverage_snapshot_id=consumed_id,
        payload_hash=consumed_hash,
    )

    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    decision = await repository.evaluate_and_seal(
        command=_gate_command(
            case,
            await pg.task_row(case.task_id),
            execution_status=TaskExecutionStatus.COMPLETED,
        )
    )

    assert decision.snapshot.gate_passed is True
    assert decision.finalization.state.value == "sealed"
    assert decision.snapshot.finalization_epoch == 1
    assert decision.snapshot.facts.h5_coverage_snapshot_id == consumed_id
    assert decision.snapshot.facts.h5_coverage_snapshot_hash == consumed_hash
    assert decision.snapshot.facts.h5_coverage_snapshot_epoch == 0

    gate_row = await pg.connection.fetchrow(
        """
        SELECT gate_snapshot_id, finalization_epoch,
               h5_coverage_snapshot_id, h5_coverage_snapshot_hash,
               h5_coverage_snapshot_epoch
        FROM qc_completion_gate_snapshot
        WHERE gate_snapshot_id = $1
        """,
        decision.snapshot.gate_snapshot_id,
    )
    assert gate_row is not None
    assert tuple(gate_row) == (
        decision.snapshot.gate_snapshot_id,
        1,
        consumed_id,
        consumed_hash,
        0,
    )

    late_id = f"h5-late-{uuid4()}"
    late_hash = "b" * 64
    await _insert_h5_snapshot(
        pg.connection,
        task_id=case.task_id,
        agent_run_id=case.run_id,
        finalization_epoch=0,
        revision=2,
        coverage_snapshot_id=late_id,
        payload_hash=late_hash,
    )

    insight = PostgresTaskInsightRepository(pool=pg.pool)
    result = await insight.fetch_h5_coverage_snapshot(
        task_id=case.task_id,
        agent_run_id=case.run_id,
    )
    assert result is not None
    assert result["coverage_snapshot_id"] == consumed_id
    assert result["payload_hash"] == consumed_hash
    assert result["revision"] == 1
    assert result["gate_snapshot_id"] == decision.snapshot.gate_snapshot_id
    assert result["gate_finalization_epoch"] == 1


@pytest.mark.asyncio
async def test_h5_insight_requires_current_epoch_and_run_without_old_fallback(
    pg: _PostgresHarness,
) -> None:
    """未绑定 Gate 时只读当前 epoch 的当前 Run，当前快照缺失就返回空。"""

    case = await _prepare_h5_case(pg)
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET finalization_epoch = 1,
            finalization_row_version = finalization_row_version + 1,
            completion_gate_snapshot_id = NULL
        WHERE id = $1::uuid
        """,
        case.task_id,
    )
    old_id = f"h5-old-{uuid4()}"
    current_id = f"h5-current-{uuid4()}"
    await _insert_h5_snapshot(
        pg.connection,
        task_id=case.task_id,
        agent_run_id=case.run_id,
        finalization_epoch=0,
        revision=9,
        coverage_snapshot_id=old_id,
        payload_hash="c" * 64,
    )
    await _insert_h5_snapshot(
        pg.connection,
        task_id=case.task_id,
        agent_run_id=case.run_id,
        finalization_epoch=1,
        revision=1,
        coverage_snapshot_id=current_id,
        payload_hash="d" * 64,
    )

    insight = PostgresTaskInsightRepository(pool=pg.pool)
    result = await insight.fetch_h5_coverage_snapshot(
        task_id=case.task_id,
        agent_run_id=case.run_id,
    )
    assert result is not None
    assert result["coverage_snapshot_id"] == current_id
    assert result["finalization_epoch"] == 1

    await pg.connection.execute(
        "DELETE FROM qc_h5_coverage_snapshot WHERE coverage_snapshot_id = $1",
        current_id,
    )
    assert (
        await insight.fetch_h5_coverage_snapshot(
            task_id=case.task_id,
            agent_run_id=case.run_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_h5_insight_rejects_cross_run_task_and_hash_identity(
    pg: _PostgresHarness,
) -> None:
    """Coverage 的 Run、Task 或 hash 身份不一致时读侧必须失败关闭。"""

    case = await _prepare_h5_case(pg)
    consumed_id = f"h5-bound-{uuid4()}"
    consumed_hash = "e" * 64
    await _insert_h5_snapshot(
        pg.connection,
        task_id=case.task_id,
        agent_run_id=case.run_id,
        finalization_epoch=0,
        revision=1,
        coverage_snapshot_id=consumed_id,
        payload_hash=consumed_hash,
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    decision = await repository.evaluate_and_seal(
        command=_gate_command(
            case,
            await pg.task_row(case.task_id),
            execution_status=TaskExecutionStatus.COMPLETED,
        )
    )

    other_case = await _prepare_h5_case(pg)
    await _insert_h5_snapshot(
        pg.connection,
        task_id=other_case.task_id,
        agent_run_id=other_case.run_id,
        finalization_epoch=0,
        revision=1,
        coverage_snapshot_id=f"h5-other-{uuid4()}",
        payload_hash="f" * 64,
    )
    insight = PostgresTaskInsightRepository(pool=pg.pool)
    assert (
        await insight.fetch_h5_coverage_snapshot(
            task_id=case.task_id,
            agent_run_id=other_case.run_id,
        )
        is None
    )
    assert (
        await insight.fetch_h5_coverage_snapshot(
            task_id=other_case.task_id,
            agent_run_id=case.run_id,
        )
        is None
    )

    pinned_insight = PostgresTaskInsightRepository(pool=_PinnedPool(pg.connection))
    async with pg.connection.transaction():
        await pg.connection.execute("SET LOCAL session_replication_role = 'replica'")
        await pg.connection.execute(
            """
            UPDATE qc_completion_gate_snapshot
            SET h5_coverage_snapshot_hash = $2
            WHERE gate_snapshot_id = $1
            """,
            decision.snapshot.gate_snapshot_id,
            "0" * 64,
        )
        assert (
            await pinned_insight.fetch_h5_coverage_snapshot(
                task_id=case.task_id,
                agent_run_id=case.run_id,
            )
            is None
        )
