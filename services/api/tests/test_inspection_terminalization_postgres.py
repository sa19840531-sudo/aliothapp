"""在隔离 PostgreSQL 中验证补采终态的页面归属、尝试上限与幂等。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 迁移显式隔离测试库
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


class _PinnedConnectionAcquire:
    """让仓储复用测试事务中的连接，避免写入测试数据库。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return False


class _PinnedConnectionPool:
    """只提供仓储所需的 acquire 接口。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    def acquire(self) -> _PinnedConnectionAcquire:
        return _PinnedConnectionAcquire(self._connection)


@pytest.mark.asyncio
async def test_terminalizes_same_rule_per_page_and_includes_exhausted_work() -> None:
    """同任务同规则的两个页面各自产生终态，耗尽页也不能遗留 waiting。"""

    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        _pool,
        ids,
    ):
        async with connection.transaction():
            for work_id, request_id, attempt_count in (
                (ids["inspection_work_item"], ids["request"], 1),
                (ids["old_inspection_work_item"], ids["old_request"], 3),
            ):
                await connection.execute(
                    """
                    UPDATE qc_inspection_work_item
                    SET status = 'waiting_evidence',
                        control_mode = 'enforced',
                        attempt_count = $2
                    WHERE id = $1::uuid
                    """,
                    work_id,
                    attempt_count,
                )
                await connection.execute(
                    """
                    INSERT INTO qc_inspection_work_item_rule(
                        work_item_id, rule_id, rule_code, ordinal, contract_hash
                    )
                    VALUES($1::uuid, $2::uuid, 'FACT-GAP-1', 0, 'terminal-test')
                    """,
                    work_id,
                    ids["rule"],
                )
                await connection.execute(
                    """
                    UPDATE qc_evidence_request
                    SET status = 'unfulfillable',
                        resolution_reason = '测试不可恢复',
                        completed_at = now()
                    WHERE request_id = $1
                    """,
                    request_id,
                )

            repository = PostgresInspectionWorkItemRepository(
                pool=_PinnedConnectionPool(connection)
            )
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0

            work_rows = await connection.fetch(
                """
                SELECT id::text, page_capture_id::text, status, attempt_count
                FROM qc_inspection_work_item
                WHERE id = ANY($1::uuid[])
                ORDER BY page_capture_id
                """,
                [ids["inspection_work_item"], ids["old_inspection_work_item"]],
            )
            assert {
                row["id"]: (row["status"], row["attempt_count"])
                for row in work_rows
            } == {
                ids["inspection_work_item"]: ("partial", 1),
                ids["old_inspection_work_item"]: ("partial", 3),
            }

            execution_rows = await connection.fetch(
                """
                SELECT execution.page_capture_id::text AS page_capture_id,
                       execution.agent_run_id::text AS agent_run_id,
                       execution.execution_status,
                       execution.source_idempotency_key
                FROM qc_rule_execution AS execution
                WHERE execution.task_id = $1::uuid
                  AND execution.rule_id = $2::uuid
                ORDER BY execution.page_capture_id
                """,
                ids["task"],
                ids["rule"],
            )
            assert len(execution_rows) == 2
            assert {row["execution_status"] for row in execution_rows} == {
                "evidence_insufficient"
            }
            assert {row["agent_run_id"] for row in execution_rows} == {ids["run"]}
            assert {
                row["source_idempotency_key"] for row in execution_rows
            } == {
                f"evidence-request-terminal:{ids['request']}",
                f"evidence-request-terminal:{ids['old_request']}",
            }

            member_rows = await connection.fetch(
                """
                SELECT work_item_id::text AS work_item_id, status,
                       rule_execution_id
                FROM qc_inspection_work_item_rule
                WHERE rule_id = $1::uuid
                ORDER BY work_item_id
                """,
                ids["rule"],
            )
            assert len(member_rows) == 2
            assert {row["status"] for row in member_rows} == {
                "evidence_insufficient"
            }
            assert all(row["rule_execution_id"] is not None for row in member_rows)

            # 第二次进入必须保持同样的两条执行结果，不得重复插入或再次推进已终态工作。
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
            assert await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_rule_execution
                WHERE task_id = $1::uuid AND rule_id = $2::uuid
                """,
                ids["task"],
                ids["rule"],
            ) == 2


@pytest.mark.asyncio
async def test_terminal_execution_deduplicates_same_capture_across_work_batches() -> None:
    """同一 capture 的不同批次共享唯一 execution，但各自成员仍正确回写。"""

    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        _pool,
        ids,
    ):
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE qc_inspection_work_item
                SET page_capture_id = $2::uuid,
                    batch_key = 'terminal-test:duplicate-capture',
                    input_hash = 'terminal-test:duplicate-capture-input',
                    status = 'waiting_evidence',
                    control_mode = 'enforced',
                    attempt_count = 1
                WHERE id = $1::uuid
                """,
                ids["old_inspection_work_item"],
                ids["capture"],
            )
            await connection.execute(
                """
                UPDATE qc_evidence_request
                SET target_capture_id = $2::uuid,
                    status = 'unfulfillable',
                    resolution_reason = '同 capture 批次测试不可恢复',
                    completed_at = now()
                WHERE request_id = $1
                """,
                ids["old_request"],
                ids["capture"],
            )
            for work_id, request_id in (
                (ids["inspection_work_item"], ids["request"]),
                (ids["old_inspection_work_item"], ids["old_request"]),
            ):
                await connection.execute(
                    """
                    UPDATE qc_inspection_work_item
                    SET status = 'waiting_evidence', control_mode = 'enforced'
                    WHERE id = $1::uuid
                    """,
                    work_id,
                )
                await connection.execute(
                    """
                    INSERT INTO qc_inspection_work_item_rule(
                        work_item_id, rule_id, rule_code, ordinal, contract_hash
                    )
                    VALUES($1::uuid, $2::uuid, 'FACT-GAP-1', 0, 'same-capture-test')
                    """,
                    work_id,
                    ids["rule"],
                )
                await connection.execute(
                    """
                    UPDATE qc_evidence_request
                    SET status = 'unfulfillable',
                        resolution_reason = '同 capture 批次测试不可恢复',
                        completed_at = now()
                    WHERE request_id = $1
                    """,
                    request_id,
                )

            repository = PostgresInspectionWorkItemRepository(
                pool=_PinnedConnectionPool(connection)
            )
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
            execution_rows = await connection.fetch(
                """
                SELECT id::text, page_capture_id::text, agent_run_id::text,
                       source_idempotency_key
                FROM qc_rule_execution
                WHERE task_id = $1::uuid AND rule_id = $2::uuid
                """,
                ids["task"],
                ids["rule"],
            )
            assert len(execution_rows) == 1
            assert execution_rows[0]["page_capture_id"] == ids["capture"]
            assert execution_rows[0]["agent_run_id"] == ids["run"]

            member_rows = await connection.fetch(
                """
                SELECT work_item_id::text AS work_item_id, status,
                       rule_execution_id::text AS rule_execution_id
                FROM qc_inspection_work_item_rule
                WHERE rule_id = $1::uuid
                ORDER BY work_item_id
                """,
                ids["rule"],
            )
            assert len(member_rows) == 2
            assert {row["status"] for row in member_rows} == {
                "evidence_insufficient"
            }
            assert {
                row["rule_execution_id"] for row in member_rows
            } == {execution_rows[0]["id"]}


@pytest.mark.asyncio
async def test_terminal_execution_does_not_cross_consumer_runs() -> None:
    """同 capture 跨 consumer Run 时不得把一个 execution 串给另一工作。"""

    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        _pool,
        ids,
    ):
        other_run_id = str(uuid4())
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO qc_agent_run(id, task_id, agent_type, agent_name)
                VALUES($1::uuid, $2::uuid, 'rule_judge', 'terminal-isolation-test')
                """,
                other_run_id,
                ids["task"],
            )
            await connection.execute(
                """
                UPDATE qc_inspection_work_item
                SET page_capture_id = $2::uuid,
                    batch_key = 'terminal-test:cross-run',
                    input_hash = 'terminal-test:cross-run-input',
                    consumer_agent_run_id = $3::uuid,
                    status = 'waiting_evidence',
                    control_mode = 'enforced',
                    attempt_count = 1
                WHERE id = $1::uuid
                """,
                ids["old_inspection_work_item"],
                ids["capture"],
                other_run_id,
            )
            await connection.execute(
                """
                UPDATE qc_evidence_request
                SET target_capture_id = $2::uuid,
                    status = 'unfulfillable',
                    resolution_reason = '跨 Run 测试不可恢复',
                    completed_at = now()
                WHERE request_id = $1
                """,
                ids["old_request"],
                ids["capture"],
            )
            for work_id, request_id in (
                (ids["inspection_work_item"], ids["request"]),
                (ids["old_inspection_work_item"], ids["old_request"]),
            ):
                await connection.execute(
                    """
                    UPDATE qc_inspection_work_item
                    SET status = 'waiting_evidence', control_mode = 'enforced'
                    WHERE id = $1::uuid
                    """,
                    work_id,
                )
                await connection.execute(
                    """
                    INSERT INTO qc_inspection_work_item_rule(
                        work_item_id, rule_id, rule_code, ordinal, contract_hash
                    )
                    VALUES($1::uuid, $2::uuid, 'FACT-GAP-1', 0, 'cross-run-test')
                    """,
                    work_id,
                    ids["rule"],
                )
                await connection.execute(
                    """
                    UPDATE qc_evidence_request
                    SET status = 'unfulfillable',
                        resolution_reason = '跨 Run 测试不可恢复',
                        completed_at = now()
                    WHERE request_id = $1
                    """,
                    request_id,
                )

            repository = PostgresInspectionWorkItemRepository(
                pool=_PinnedConnectionPool(connection)
            )
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
            rows = await connection.fetch(
                """
                SELECT id::text, consumer_agent_run_id::text, status
                FROM qc_inspection_work_item
                WHERE id = ANY($1::uuid[])
                """,
                [ids["inspection_work_item"], ids["old_inspection_work_item"]],
            )
            work_states = {row["consumer_agent_run_id"]: row["status"] for row in rows}
            assert work_states[ids["run"]] == "partial"
            assert work_states[other_run_id] == "waiting_evidence"

            member_rows = await connection.fetch(
                """
                SELECT member.work_item_id::text AS work_item_id,
                       member.status,
                       member.rule_execution_id::text AS rule_execution_id
                FROM qc_inspection_work_item_rule AS member
                WHERE member.rule_id = $1::uuid
                """,
                ids["rule"],
            )
            assert len(member_rows) == 2
            by_work = {row["work_item_id"]: row for row in member_rows}
            assert by_work[ids["inspection_work_item"]]["status"] == (
                "evidence_insufficient"
            )
            assert by_work[ids["old_inspection_work_item"]]["status"] == "pending"
            assert by_work[ids["old_inspection_work_item"]]["rule_execution_id"] is None

            original_review_reason = await connection.fetchval(
                "SELECT review_reason FROM qc_review_item WHERE task_id = $1::uuid",
                ids["task"],
            )
            # 即使另一 Run 的请求本身合法，也不能改写已存在 execution 的复核原因。
            await connection.execute(
                "UPDATE qc_inspection_attempt SET consumer_agent_run_id = $2::uuid "
                "WHERE work_item_id = $1::uuid",
                ids["old_inspection_work_item"], other_run_id,
            )
            await connection.execute(
                "UPDATE qc_evidence_request SET resolution_reason = '另一运行的原因' "
                "WHERE request_id = $1",
                ids["old_request"],
            )
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
            assert await connection.fetchval(
                "SELECT review_reason FROM qc_review_item WHERE task_id = $1::uuid",
                ids["task"],
            ) == original_review_reason
            assert await connection.fetchval(
                "SELECT rule_execution_id FROM qc_inspection_work_item_rule "
                "WHERE work_item_id = $1::uuid AND rule_id = $2::uuid",
                ids["old_inspection_work_item"], ids["rule"],
            ) is None
