import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)


class _BorrowedConnectionPool:
    """让仓储复用外层回滚事务，不额外占用或提交连接。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


async def test_web_stage_duration_query_parses_against_migrated_postgres() -> None:
    """真实 PG 负责发现单元桩无法识别的表名、列名或类型错误。"""

    database_url = os.environ.get("ALIOTH_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("需显式设置 Web V2 隔离数据库")
    pool = await asyncpg.create_pool(
        database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
        min_size=1,
        max_size=1,
    )
    try:
        subject_id = str(uuid4())
        result = await PostgresWorkstationQueryRepository(pool=pool).get_web_stage_durations(
            str(uuid4()),
            agent_run_id=None,
            access_scope=AccessScope(
                actor_user_id=subject_id,
                tenant_id=subject_id,
                permissions=frozenset({"taskObjects:read:any"}),
            ),
        )
    finally:
        await pool.close()

    assert result is None


async def test_web_stage_duration_query_uses_real_distinct_persisted_anchors() -> None:
    """真实 PG 用不同时间锚点防止 Coverage、Gate 或 Binding 被误当阶段边界。"""

    database_url = os.environ.get("ALIOTH_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("需显式设置 Web V2 隔离数据库")
    normalized_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    database_name = urlparse(normalized_url).path.rsplit("/", 1)[-1].casefold()
    if "test" not in database_name:
        raise RuntimeError("Web 阶段耗时 PG 测试只允许连接名称含 test 的隔离数据库")

    pool = await asyncpg.create_pool(normalized_url, min_size=1, max_size=1)
    task_id = str(uuid4())
    run_id = str(uuid4())
    ruleset_id = str(uuid4())
    rule_id = str(uuid4())
    task_created_at = datetime(2026, 9, 3, tzinfo=UTC)
    async with pool.acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        try:
            principal = await connection.fetchrow(
                "SELECT id::text AS user_id, tenant_id::text AS tenant_id "
                "FROM qc_user ORDER BY created_at LIMIT 1"
            )
            if principal is None:
                raise RuntimeError("隔离测试库缺少迁移创建的历史主体")
            await connection.execute(
                """
                INSERT INTO qc_ruleset (
                    id, ruleset_code, name, version, status
                ) VALUES ($1::uuid, $2, 'stage duration test', 1, 'enabled')
                """,
                ruleset_id,
                f"stage-duration-{ruleset_id}",
            )
            await connection.execute(
                """
                INSERT INTO qc_rule (
                    id, ruleset_id, rule_code, name, category,
                    executor_type, severity, status, check_point
                ) VALUES (
                    $1::uuid, $2::uuid, $3, 'stage rule', 'text',
                    'rule_engine', 'low', 'enabled', 'stage anchor'
                )
                """,
                rule_id,
                ruleset_id,
                f"stage-rule-{rule_id}",
            )
            await connection.execute(
                """
                INSERT INTO qc_task (
                    id, target_url, ruleset_id, channel, status,
                    submitter_id, tenant_id, target_platform, task_config,
                    finalization_epoch, created_at
                ) VALUES (
                    $1::uuid, 'https://stage.test/', $2::uuid, 'workbench', 'running',
                    $3::uuid, $4::uuid, 'web',
                    '{"workflow_version":"v2","execution_mode":"full_inspection"}'::jsonb,
                    7, $5
                )
                """,
                task_id,
                ruleset_id,
                principal["user_id"],
                principal["tenant_id"],
                task_created_at,
            )
            await connection.execute(
                """
                INSERT INTO qc_agent_run (
                    id, task_id, agent_type, agent_name, status,
                    started_at, finished_at, created_at
                ) VALUES (
                    $1::uuid, $2::uuid, 'main', 'web-main', 'done',
                    $3, $4, $3
                )
                """,
                run_id,
                task_id,
                task_created_at + timedelta(minutes=1),
                task_created_at + timedelta(minutes=6),
            )
            await connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id = $2::uuid "
                "WHERE id = $1::uuid",
                task_id,
                run_id,
            )
            await connection.execute(
                """
                INSERT INTO qc_web_coverage_snapshot (
                    task_id, agent_run_id, finalization_epoch,
                    required_page_types, observed_page_types,
                    missing_page_types, unrecognized_page_types,
                    normalization_version, coverage_status, snapshot_hash, created_at
                ) VALUES (
                    $1::uuid, $2::uuid, 7,
                    '["home"]'::jsonb, '["home"]'::jsonb,
                    '[]'::jsonb, '[]'::jsonb,
                    'web-page-type-assessment.v2', 'complete', $3, $4
                )
                """,
                task_id,
                run_id,
                "a" * 64,
                task_created_at + timedelta(minutes=4),
            )
            await connection.execute(
                """
                INSERT INTO qc_rule_execution (
                    task_id, rule_id, agent_run_id, execution_status,
                    judge_strategy, started_at, finished_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, 'pass',
                    'rule_engine', $4, $5
                )
                """,
                task_id,
                rule_id,
                run_id,
                task_created_at + timedelta(minutes=2),
                task_created_at + timedelta(minutes=5),
            )
            await connection.executemany(
                """
                INSERT INTO qc_web_report_generation_attempt (
                    id, task_id, agent_run_id, finalization_epoch,
                    attempt_number, status, failure_code, started_at, finished_at
                ) VALUES ($1::uuid, $2::uuid, $3::uuid, 7, $4, $5, $6, $7, $8)
                """,
                [
                    (
                        str(uuid4()),
                        task_id,
                        run_id,
                        1,
                        "failed",
                        "worker_interrupted",
                        task_created_at + timedelta(minutes=6, seconds=30),
                        task_created_at + timedelta(minutes=7),
                    ),
                    (
                        str(uuid4()),
                        task_id,
                        run_id,
                        2,
                        "succeeded",
                        None,
                        task_created_at + timedelta(minutes=7, seconds=30),
                        task_created_at + timedelta(minutes=8),
                    ),
                ],
            )
            gate_snapshot_id = f"stage-gate-{uuid4()}"
            await connection.execute(
                """
                INSERT INTO qc_completion_gate_snapshot (
                    gate_snapshot_id, task_id, finalization_epoch,
                    evaluation_sequence, protocol_version, facts_hash,
                    exploration_terminal, no_pending_action,
                    no_pending_capture_commit, no_nonterminal_enforced_work_item,
                    no_active_session_request, candidate_rule_coverage_terminal,
                    no_background_execution, review_policy_satisfied,
                    pending_action_count, pending_capture_commit_count,
                    nonterminal_enforced_work_item_count, active_session_request_count,
                    candidate_rule_count, terminal_candidate_rule_count,
                    background_execution_count, open_review_count,
                    review_generation, block_reasons, gate_passed, evaluated_at
                ) VALUES (
                    $1, $2::uuid, 7, 1, 'task-completion-gate.v1', $3,
                    true, true, true, true, true, true, true, true,
                    0, 0, 0, 0, 1, 1, 0, 0, 0, '[]'::jsonb, true, $4
                )
                """,
                gate_snapshot_id,
                task_id,
                "b" * 64,
                task_created_at + timedelta(minutes=9),
            )
            report_id = f"stage-report-{uuid4()}"
            report_hash = "c" * 64
            await connection.execute(
                """
                INSERT INTO qc_report_gate_binding (
                    report_id, report_version, report_payload_hash,
                    protocol_version, task_id, gate_snapshot_id,
                    finalization_epoch, execution_status, quality_outcome, bound_at
                ) VALUES (
                    $1, 1, $2, 'report-gate-binding.v1', $3::uuid, $4,
                    7, 'completed', 'pass', $5
                )
                """,
                report_id,
                report_hash,
                task_id,
                gate_snapshot_id,
                task_created_at + timedelta(minutes=10),
            )
            await connection.execute(
                """
                INSERT INTO qc_report_payload_snapshot (
                    report_id, report_version, report_payload_hash, report_payload
                ) VALUES (
                    $1, 1, $2,
                    jsonb_build_object('agent_run_id', $3::text)
                )
                """,
                report_id,
                report_hash,
                run_id,
            )

            durations = await PostgresWorkstationQueryRepository(
                pool=_BorrowedConnectionPool(connection)
            ).get_web_stage_durations(
                task_id,
                agent_run_id=run_id,
                access_scope=AccessScope(
                    actor_user_id=str(uuid4()),
                    tenant_id=str(uuid4()),
                    permissions=frozenset({"taskObjects:read:any"}),
                ),
            )
            pending_execution_id = await connection.fetchval(
                """
                INSERT INTO qc_rule_execution (
                    task_id, rule_id, agent_run_id, execution_status,
                    judge_strategy, started_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, 'pass',
                    'rule_engine', $4
                )
                RETURNING id::text
                """,
                task_id,
                rule_id,
                run_id,
                task_created_at + timedelta(minutes=4, seconds=30),
            )
            mixed_state_durations = await PostgresWorkstationQueryRepository(
                pool=_BorrowedConnectionPool(connection)
            ).get_web_stage_durations(
                task_id,
                agent_run_id=run_id,
                access_scope=AccessScope(
                    actor_user_id=str(uuid4()),
                    tenant_id=str(uuid4()),
                    permissions=frozenset({"taskObjects:read:any"}),
                ),
            )
            assert pending_execution_id is not None
            update_result = await connection.execute(
                """
                UPDATE qc_rule_execution
                SET finished_at = $2
                WHERE id = $1::uuid
                """,
                pending_execution_id,
                task_created_at + timedelta(minutes=5, seconds=15),
            )
            assert update_result == "UPDATE 1"
            await connection.execute(
                """
                INSERT INTO qc_aggregate_inspection_candidate (
                    task_id, source_agent_run_id, rule_id, rule_code,
                    rule_contract_hash, scope, scope_key,
                    finalization_epoch, status, expected_member_count
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4,
                    $5, 'task', $6, 7, 'expected', 1
                )
                """,
                task_id,
                run_id,
                rule_id,
                f"stage-rule-{rule_id}",
                "d" * 64,
                f"stage_duration_{uuid4().hex}",
            )
            mixed_candidate_durations = await PostgresWorkstationQueryRepository(
                pool=_BorrowedConnectionPool(connection)
            ).get_web_stage_durations(
                task_id,
                agent_run_id=run_id,
                access_scope=AccessScope(
                    actor_user_id=str(uuid4()),
                    tenant_id=str(uuid4()),
                    permissions=frozenset({"taskObjects:read:any"}),
                ),
            )
            interrupted_attempt_id = str(uuid4())
            await connection.execute(
                """
                INSERT INTO qc_web_report_generation_attempt (
                    id, task_id, agent_run_id, finalization_epoch,
                    attempt_number, status, started_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, 7, 3, 'running',
                    clock_timestamp() - interval '10 seconds'
                )
                """,
                interrupted_attempt_id,
                task_id,
                run_id,
            )
            mixed_report_durations = await PostgresWorkstationQueryRepository(
                pool=_BorrowedConnectionPool(connection)
            ).get_web_stage_durations(
                task_id,
                agent_run_id=run_id,
                access_scope=AccessScope(
                    actor_user_id=str(uuid4()),
                    tenant_id=str(uuid4()),
                    permissions=frozenset({"taskObjects:read:any"}),
                ),
            )
        finally:
            await transaction.rollback()
    await pool.close()

    assert durations is not None
    assert durations.to_dict() == {
        "exploration_ms": 300_000,
        "inspection_ms": 180_000,
        "report_generation_ms": 90_000,
        "total_ms": 600_000,
    }
    assert mixed_state_durations is not None
    assert mixed_state_durations.to_dict() == {
        "exploration_ms": 300_000,
        "inspection_ms": None,
        "report_generation_ms": 90_000,
        "total_ms": 600_000,
    }
    assert mixed_candidate_durations is not None
    assert mixed_candidate_durations.inspection_ms is None
    assert mixed_report_durations is not None
    assert mixed_report_durations.report_generation_ms is None
