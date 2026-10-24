"""F01 任务收口恢复的最小真实 PostgreSQL 回归。

这些用例只接受显式的 ``ALIOTH_TEST_DATABASE_URL``，并在用例会话开始时把
当前 Alembic head 完整升级到隔离库。Web V2 正式 Coverage 链仍由现有正式报告
集成测试覆盖；这里使用带真实 Gate SQL 和报告绑定的非正式 Web v1 任务，专门
验证审核唤醒、历史状态恢复、租约 fencing 以及阻塞快照复用。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.application.task_finalization import (
    EvaluateCompletionGateCommand,
    FinalizeTaskWithReportCommand,
    RecordReviewPolicyDecisionCommand,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_review_repository import (
    PostgresWorkstationReviewRepository,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"


def _test_database_url() -> str:
    """只允许本机、postgres 角色和名称含 test 的显式数据库。"""

    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("F01 PG 回归只允许连接 localhost 隔离库")
    database_name = parsed.path.strip("/").lower()
    if "test" not in database_name:
        raise ValueError("F01 PG 回归数据库名必须包含 test")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
        allowed_roles=frozenset({"postgres"}),
    )


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    """将显式测试库升级到当前迁移版本；新建空库时执行完整迁移。"""

    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        with patch("logging.config.fileConfig"):
            command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


@dataclass(frozen=True)
class _Case:
    tenant_id: str
    user_id: str
    ruleset_id: str
    rule_id: str
    task_id: str
    run_id: str
    capture_id: str | None = None
    execution_id: str | None = None
    review_id: str | None = None


class _PostgresHarness:
    """用随机主键构造最小任务图，并只清理本文件创建的对象。"""

    def __init__(self, connection: asyncpg.Connection, pool: asyncpg.Pool) -> None:
        self.connection = connection
        self.pool = pool
        self.cases: list[_Case] = []

    async def seed_case(
        self,
        *,
        formal: bool = False,
        with_review: bool = False,
        run_status: str = "done",
        task_status: str = "review",
        session_draining: bool = True,
        include_completed_exploration: bool = False,
        target_platform: str = "web",
    ) -> _Case:
        ids = {name: str(uuid4()) for name in (
            "tenant", "user", "ruleset", "rule", "task", "run", "capture",
            "execution", "review",
        )}
        task_config = (
            {
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "web_inspection_notification_mode": "enforced",
            }
            if formal
            else {"workflow_version": "v1"}
        )
        await self.connection.execute(
            """
            INSERT INTO qc_tenant(id, tenant_key, display_name)
            VALUES($1::uuid, $2, $3)
            """,
            ids["tenant"],
            f"f01-{ids['tenant']}",
            f"F01 PG tenant {ids['tenant']}",
        )
        await self.connection.execute(
            """
            INSERT INTO qc_user(
                id, username, full_name, password_hash, role, tenant_id
            )
            VALUES($1::uuid, $2, 'F01 PG reviewer', 'integration-only',
                   '平台管理员', $3::uuid)
            """,
            ids["user"],
            f"f01-{ids['user']}",
            ids["tenant"],
        )
        await self.connection.execute(
            """
            INSERT INTO qc_ruleset(
                id, ruleset_code, name, version, status, source_type, rule_count,
                page_sampling_policy, loop_guard
            )
            VALUES($1::uuid, $2, 'F01 PG ruleset', 1, 'enabled', 'manual', 1,
                   '{}'::jsonb, '{}'::jsonb)
            """,
            ids["ruleset"],
            f"f01-rules-{ids['ruleset']}",
        )
        await self.connection.execute(
            """
            INSERT INTO qc_rule(
                id, ruleset_id, rule_code, name, category, executor_type,
                check_point
            )
            VALUES($1::uuid, $2::uuid, $3, 'F01 PG rule', 'text', 'hybrid', '复核')
            """,
            ids["rule"],
            ids["ruleset"],
            f"F01-{ids['rule']}",
        )
        await self.connection.execute(
            """
            INSERT INTO qc_task(
                id, target_url, ruleset_id, submitter_id, tenant_id,
                target_platform, status, task_config
            )
            VALUES($1::uuid, 'https://f01.example.test/', $2::uuid, $3::uuid,
                   $4::uuid, $7, $5, $6::jsonb)
            """,
            ids["task"],
            ids["ruleset"],
            ids["user"],
            ids["tenant"],
            task_status,
            json.dumps(task_config),
            target_platform,
        )
        await self.connection.execute(
            """
            INSERT INTO qc_agent_run(
                id, task_id, agent_type, agent_name, status, finished_at
            )
            VALUES($1::uuid, $2::uuid, 'main', 'f01-main', $3::varchar,
                   CASE WHEN $3::varchar = 'done' THEN now() ELSE NULL END)
            """,
            ids["run"],
            ids["task"],
            run_status,
        )
        await self.connection.execute(
            """
            UPDATE qc_task
            SET finalization_agent_run_id = $2::uuid
            WHERE id = $1::uuid
            """,
            ids["task"],
            ids["run"],
        )
        capture_id: str | None = None
        if include_completed_exploration:
            capture_id = ids["capture"]
            now = datetime.now(UTC)
            await self.connection.execute(
                """
                INSERT INTO qc_page_capture(
                    id, task_id, page_seq, page_slug, url, capture_status,
                    captured_at
                )
                VALUES($1::uuid, $2::uuid, 1, $3, 'https://f01.example.test/',
                       'captured', $4)
                """,
                capture_id,
                ids["task"],
                f"f01-page-{ids['task']}",
                now,
            )
            await self.connection.execute(
                """
                INSERT INTO qc_v2_work_item(
                    work_id, work_type, task_id, session_id, branch_id,
                    source_capture_id, protocol_version, payload_hash,
                    work_payload, status, deadline, finished_at
                )
                VALUES($1, 'exploration', $2::uuid, $3, $4, $5::uuid,
                       'exploration-work.v1', $6, '{}'::jsonb, 'succeeded',
                       $7, $7)
                """,
                f"f01-exploration-{ids['task']}",
                ids["task"],
                f"f01-session-{ids['task']}",
                f"f01-branch-{ids['task']}",
                capture_id,
                f"f01-exploration-hash-{ids['task']}",
                now,
            )
        execution_id: str | None = None
        review_id: str | None = None
        if with_review:
            execution_id = ids["execution"]
            review_id = ids["review"]
            now = datetime.now(UTC)
            await self.connection.execute(
                """
                INSERT INTO qc_rule_execution(
                    id, task_id, rule_id, agent_run_id, execution_status,
                    judge_strategy, review_status, judge_input, judge_output,
                    started_at, finished_at
                )
                VALUES($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'manual_review',
                       'hybrid', 'pending', '{}'::jsonb, '{}'::jsonb, $5, $5)
                """,
                execution_id,
                ids["task"],
                ids["rule"],
                ids["run"],
                now,
            )
            await self.connection.execute(
                """
                INSERT INTO qc_review_item(
                    id, rule_execution_id, task_id, trigger_reason, status
                )
                VALUES($1::uuid, $2::uuid, $3::uuid, 'manual_flag', 'pending')
                """,
                review_id,
                execution_id,
                ids["task"],
            )
        if session_draining:
            await self.connection.execute(
                """
                UPDATE qc_task
                SET status = $2,
                    finalization_state = 'session_draining',
                    exploration_draining_at = now(),
                    session_draining_at = now()
                WHERE id = $1::uuid
                """,
                ids["task"],
                task_status,
            )
        case = _Case(
            tenant_id=ids["tenant"],
            user_id=ids["user"],
            ruleset_id=ids["ruleset"],
            rule_id=ids["rule"],
            task_id=ids["task"],
            run_id=ids["run"],
            capture_id=capture_id,
            execution_id=execution_id,
            review_id=review_id,
        )
        self.cases.append(case)
        return case

    async def task_row(self, task_id: str) -> asyncpg.Record:
        row = await self.connection.fetchrow(
            """
            SELECT status, finalization_state, finalization_epoch,
                   finalization_row_version AS row_version,
                   finalization_retry_at, completion_gate_snapshot_id,
                   finalization_retry_at = 'infinity'::timestamptz
                       AS finalization_waits_for_event,
                   review_generation, now() AS database_now
            FROM qc_task WHERE id = $1::uuid
            """,
            task_id,
        )
        assert row is not None
        return row

    async def cleanup(self) -> None:
        """报告正文先删，再按外键顺序删除本测试产生的随机任务。"""

        task_ids = [case.task_id for case in self.cases]
        if not task_ids:
            return
        async with self.connection.transaction():
            await self.connection.execute("SET LOCAL session_replication_role = 'replica'")
            await self.connection.execute(
                """
                DELETE FROM qc_report_payload_snapshot
                WHERE report_id IN (
                    SELECT binding.report_id
                    FROM qc_report_gate_binding AS binding
                    WHERE binding.task_id = ANY($1::uuid[])
                )
                """,
                task_ids,
            )
            await self.connection.execute(
                "DELETE FROM qc_report_gate_binding WHERE task_id = ANY($1::uuid[])",
                task_ids,
            )
            # replica 只用于清理本测试的不可变审计数据，同时会禁用 FK 级联。
            # 因此显式清理所创建的每类 Task 子记录，不能只删 Task 留下孤儿。
            for table in (
                "qc_review_policy_gate_decision", "qc_completion_gate_snapshot",
                "qc_review_item", "qc_rule_execution", "qc_v2_work_item",
                "qc_capture", "qc_page_capture", "qc_agent_run",
            ):
                await self.connection.execute(
                    f"DELETE FROM {table} WHERE task_id = ANY($1::uuid[])", task_ids,
                )
            await self.connection.execute(
                "DELETE FROM qc_task WHERE id = ANY($1::uuid[])", task_ids
            )
            await self.connection.execute(
                "DELETE FROM qc_rule WHERE id = ANY($1::uuid[])",
                [case.rule_id for case in self.cases],
            )
            await self.connection.execute(
                "DELETE FROM qc_ruleset WHERE id = ANY($1::uuid[])",
                [case.ruleset_id for case in self.cases],
            )
            await self.connection.execute(
                "DELETE FROM qc_user WHERE id = ANY($1::uuid[])",
                [case.user_id for case in self.cases],
            )
            await self.connection.execute(
                "DELETE FROM qc_tenant WHERE id = ANY($1::uuid[])",
                [case.tenant_id for case in self.cases],
            )


@pytest.fixture
async def pg() -> _PostgresHarness:
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    harness = _PostgresHarness(connection, pool)
    try:
        yield harness
    finally:
        await harness.cleanup()
        await pool.close()
        await connection.close()


def _scope(case: _Case) -> AccessScope:
    return AccessScope(
        actor_user_id=case.user_id,
        tenant_id=case.tenant_id,
        permissions=frozenset({"taskObjects:review:tenant"}),
    )


def _gate_command(
    case: _Case,
    row: asyncpg.Record,
    *,
    execution_status: TaskExecutionStatus = TaskExecutionStatus.NEEDS_REVIEW,
) -> EvaluateCompletionGateCommand:
    return EvaluateCompletionGateCommand(
        task_id=case.task_id,
        agent_run_id=case.run_id,
        expected_finalization_epoch=int(row["finalization_epoch"]),
        expected_row_version=int(row["row_version"]),
        execution_status=execution_status,
        quality_outcome=TaskQualityOutcome.UNKNOWN,
    )


async def _assert_retry_due(harness: _PostgresHarness, task_id: str) -> None:
    row = await harness.task_row(task_id)
    assert row["finalization_retry_at"] is not None
    assert row["finalization_retry_at"] <= row["database_now"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ("workstation", "insight"))
async def test_manual_review_wakes_gate_and_gate_owns_done_and_report_binding(
    pg: _PostgresHarness,
    entrypoint: str,
) -> None:
    """两个真实人工入口均只唤醒 Gate，最后由 Gate/报告事务完成封存。"""

    case = await pg.seed_case(
        with_review=True,
        task_status="review",
        include_completed_exploration=True,
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    blocked = await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    assert blocked.snapshot.gate_passed is False
    assert blocked.finalization.state.value == "session_draining"
    assert blocked.snapshot.facts.open_review_count == 1
    blocked_row = await pg.task_row(case.task_id)
    assert blocked_row["status"] == "review"
    assert blocked_row["finalization_waits_for_event"] is True

    if entrypoint == "workstation":
        assert case.review_id is not None
        result = await PostgresWorkstationReviewRepository(pool=pg.pool).update_review_status(
            review_id=case.review_id,
            status="approved",
            reason="F01 人工确认",
            access_scope=_scope(case),
        )
    else:
        assert case.execution_id is not None
        result = await PostgresTaskInsightRepository(pool=pg.pool).update_human_verdict(
            execution_id=case.execution_id,
            verdict="pass",
            note="F01 人工确认",
            operator=case.user_id,
            task_id=case.task_id,
            access_scope=_scope(case),
        )
    assert result is not None
    after_review = await pg.task_row(case.task_id)
    assert after_review["status"] == "review"
    assert after_review["finalization_state"] == "session_draining"
    assert after_review["review_generation"] > blocked_row["review_generation"]
    await _assert_retry_due(pg, case.task_id)
    assert case.task_id in {
        item.task_id for item in await repository.list_recoverable_finalizations(limit=100)
    }
    review_state = await pg.connection.fetchrow(
        """
        SELECT review.status, execution.human_verdict, execution.review_status
        FROM qc_review_item AS review
        JOIN qc_rule_execution AS execution
          ON execution.id = review.rule_execution_id
        WHERE review.id = $1::uuid
        """,
        case.review_id,
    )
    assert review_state is not None
    assert tuple(review_state) == ("approved", "pass", "approved")

    final_row = await pg.task_row(case.task_id)
    finalized = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=case.task_id,
            agent_run_id=case.run_id,
            report_id=f"f01-report-{case.task_id}",
            report_version=1,
            expected_finalization_epoch=int(final_row["finalization_epoch"]),
            expected_row_version=int(final_row["row_version"]),
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"summary": {"overall_status": "unknown"}},
        )
    )
    assert finalized.snapshot.gate_passed is True
    assert finalized.finalization.state.value == "sealed"
    sealed = await pg.task_row(case.task_id)
    assert sealed["status"] == "done"
    assert sealed["finalization_state"] == "sealed"
    counts = await pg.connection.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM qc_report_gate_binding WHERE task_id = $1::uuid)
                AS binding_count,
            (SELECT count(*)
             FROM qc_report_payload_snapshot AS payload
             JOIN qc_report_gate_binding AS binding
               ON binding.report_id = payload.report_id
              AND binding.report_version = payload.report_version
             WHERE binding.task_id = $1::uuid) AS payload_count,
            (SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id = $1::uuid)
                AS gate_count
        """,
        case.task_id,
    )
    assert counts is not None
    assert tuple(counts) == (1, 1, 2)
    binding = await pg.connection.fetchrow(
        """SELECT gate_snapshot_id, finalization_epoch
           FROM qc_report_gate_binding WHERE task_id = $1::uuid""",
        case.task_id,
    )
    assert tuple(binding) == (
        finalized.snapshot.gate_snapshot_id, sealed["finalization_epoch"],
    )
    assert sealed["completion_gate_snapshot_id"] == finalized.snapshot.gate_snapshot_id


@pytest.mark.asyncio
async def test_done_session_draining_with_unchanged_block_is_rechecked_and_can_seal(
    pg: _PostgresHarness,
) -> None:
    """历史 done 记录不能快返旧阻塞快照，重评后可经审核和报告正常封存。"""

    case = await pg.seed_case(
        with_review=True,
        task_status="review",
        include_completed_exploration=True,
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    first = await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    old_row = await pg.task_row(case.task_id)
    await pg.connection.execute(
        "UPDATE qc_task SET status = 'done' WHERE id = $1::uuid",
        case.task_id,
    )
    repaired = await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    assert repaired.snapshot.gate_passed is False
    assert repaired.finalization.state.value == "session_draining"
    assert repaired.finalization.gate_snapshot_id != first.finalization.gate_snapshot_id
    assert (await pg.task_row(case.task_id))["status"] == "review"
    gate_sequences = await pg.connection.fetchval(
        "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id = $1::uuid",
        case.task_id,
    )
    assert gate_sequences == 2

    assert case.review_id is not None
    assert await PostgresWorkstationReviewRepository(pool=pg.pool).update_review_status(
        review_id=case.review_id,
        status="approved",
        reason="F01 修复历史 done",
        access_scope=_scope(case),
    )
    final_row = await pg.task_row(case.task_id)
    final = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=case.task_id,
            agent_run_id=case.run_id,
            report_id=f"f01-old-done-report-{case.task_id}",
            report_version=1,
            expected_finalization_epoch=int(final_row["finalization_epoch"]),
            expected_row_version=int(final_row["row_version"]),
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"summary": {"overall_status": "unknown"}},
        )
    )
    assert final.snapshot.gate_passed is True
    assert (await pg.task_row(case.task_id))["finalization_state"] == "sealed"
    assert old_row["completion_gate_snapshot_id"] == first.snapshot.gate_snapshot_id


@pytest.mark.asyncio
async def test_recovery_list_and_claim_restore_null_and_done_but_filter_invalid_rows(
    pg: _PostgresHarness,
) -> None:
    """NULL 旧停轮和 done/session_draining 可恢复，其余状态必须留在队列外。"""

    legacy_null = await pg.seed_case(
        formal=False,
        with_review=True,
        include_completed_exploration=True,
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    blocked = await repository.evaluate_and_seal(
        command=_gate_command(legacy_null, await pg.task_row(legacy_null.task_id))
    )
    assert blocked.snapshot.gate_passed is False
    await pg.connection.execute(
        "UPDATE qc_task SET finalization_retry_at = NULL WHERE id = $1::uuid",
        legacy_null.task_id,
    )
    legacy_done = await pg.seed_case(
        formal=False,
        task_status="done",
        session_draining=True,
    )
    listed = await PostgresTaskFinalizationRepository(pool=pg.pool).list_recoverable_finalizations(
        limit=20
    )
    listed_ids = {item.task_id for item in listed}
    assert {legacy_null.task_id, legacy_done.task_id} <= listed_ids

    good = await pg.seed_case(formal=True, task_status="running", session_draining=False)
    formal_done = await pg.seed_case(formal=True, task_status="done")
    formal_blocked = await repository.evaluate_and_seal(
        command=_gate_command(formal_done, await pg.task_row(formal_done.task_id))
    )
    assert formal_blocked.snapshot.gate_passed is False
    await pg.connection.execute(
        "UPDATE qc_task SET status = 'done', finalization_retry_at = NULL WHERE id = $1::uuid",
        formal_done.task_id,
    )
    future = await pg.seed_case(formal=True, task_status="running", session_draining=False)
    quarantined = await pg.seed_case(formal=True, task_status="running", session_draining=False)
    cancelled = await pg.seed_case(formal=True, task_status="canceled", session_draining=False)
    active_owner = await pg.seed_case(
        formal=True,
        task_status="running",
        session_draining=False,
        run_status="running",
    )
    old_lease = await pg.seed_case(formal=True, task_status="running", session_draining=False)
    await pg.connection.execute(
        "UPDATE qc_task SET finalization_retry_at = now() + interval '1 hour' WHERE id = $1::uuid",
        future.task_id,
    )
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET finalization_recovery_state = 'quarantined',
            finalization_error_signature = repeat('a', 64),
            finalization_same_error_count = 3,
            finalization_quarantined_at = now()
        WHERE id = $1::uuid
        """,
        quarantined.task_id,
    )
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET finalization_recovery_lease_token = $2::uuid,
            finalization_recovery_lease_owner = 'old-worker',
            finalization_recovery_lease_expires_at = now() + interval '1 hour'
        WHERE id = $1::uuid
        """,
        old_lease.task_id,
        str(uuid4()),
    )
    leases = await PostgresTaskFinalizationRepository(pool=pg.pool).claim_recoverable_finalizations(
        limit=20,
        lease_owner="f01-claim-worker",
        lease_seconds=60,
    )
    claimed_ids = {lease.task_id for lease in leases}
    assert claimed_ids == {good.task_id, formal_done.task_id}
    assert future.task_id not in claimed_ids
    assert quarantined.task_id not in claimed_ids
    assert cancelled.task_id not in claimed_ids
    assert active_owner.task_id not in claimed_ids
    assert old_lease.task_id not in claimed_ids


@pytest.mark.asyncio
async def test_expired_old_lease_is_reclaimed_but_old_holder_cannot_write(
    pg: _PostgresHarness,
) -> None:
    """租约过期后可由新 worker 接管，旧 worker 的错误写入必须成为 no-op。"""

    case = await pg.seed_case(formal=True, task_status="running", session_draining=False)
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    first = (
        await repository.claim_recoverable_finalizations(
            limit=1, lease_owner="f01-old-worker", lease_seconds=60
        )
    )[0]
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET finalization_retry_at = now() - interval '1 second',
            finalization_recovery_lease_expires_at = now() - interval '1 second'
        WHERE id = $1::uuid
        """,
        case.task_id,
    )
    second = (
        await repository.claim_recoverable_finalizations(
            limit=1, lease_owner="f01-new-worker", lease_seconds=60
        )
    )[0]
    assert second.lease_token != first.lease_token
    assert second.recovery_generation > first.recovery_generation
    before = await pg.task_row(case.task_id)
    with pytest.raises(TaskFinalizationPersistenceError, match="租约"):
        await repository.defer_unchanged_completion_gate(
            command=replace(
                _gate_command(case, before),
                recovery_lease_token=first.lease_token,
                recovery_generation=first.recovery_generation,
                recovery_lease_expires_at=first.lease_expires_at,
            )
        )
    assert (await pg.task_row(case.task_id))["finalization_retry_at"] == before[
        "finalization_retry_at"
    ]
    await repository.record_finalization_retry_error(
        task_id=case.task_id,
        error_message="旧 worker 不应写入",
        lease_token=first.lease_token,
        recovery_generation=first.recovery_generation,
        expected_status=first.expected_status,
    )
    row = await pg.connection.fetchrow(
        """
        SELECT finalization_last_error, finalization_recovery_generation,
               finalization_recovery_lease_token::text AS lease_token
        FROM qc_task WHERE id = $1::uuid
        """,
        case.task_id,
    )
    assert row is not None
    assert row["finalization_last_error"] is None
    assert int(row["finalization_recovery_generation"]) == second.recovery_generation
    assert row["lease_token"] == second.lease_token


@pytest.mark.asyncio
async def test_unchanged_gate_snapshot_is_reused_but_changed_facts_cannot_skip(
    pg: _PostgresHarness,
) -> None:
    """人工复核同事实等待事件，审核事实变化后必须立即唤醒。"""

    case = await pg.seed_case(
        with_review=True,
        task_status="review",
        include_completed_exploration=True,
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    first = await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    first_row = await pg.task_row(case.task_id)
    unchanged = await repository.defer_unchanged_completion_gate(
        command=_gate_command(case, first_row)
    )
    assert unchanged is True
    unchanged_row = await pg.task_row(case.task_id)
    assert unchanged_row["completion_gate_snapshot_id"] == first.snapshot.gate_snapshot_id
    assert unchanged_row["finalization_waits_for_event"] is True
    assert await pg.connection.fetchval(
        "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id = $1::uuid",
        case.task_id,
    ) == 1

    assert case.execution_id is not None
    assert await PostgresTaskInsightRepository(pool=pg.pool).update_human_verdict(
        execution_id=case.execution_id,
        verdict="pass",
        note="F01 facts changed",
        operator=case.user_id,
        task_id=case.task_id,
        access_scope=_scope(case),
    )
    changed_row = await pg.task_row(case.task_id)
    assert changed_row["finalization_retry_at"] <= changed_row["database_now"]
    skipped = await repository.defer_unchanged_completion_gate(
        command=_gate_command(case, changed_row)
    )
    assert skipped is False
    assert await pg.connection.fetchval(
        "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id = $1::uuid",
        case.task_id,
    ) == 1


@pytest.mark.asyncio
async def test_h5_unchanged_gate_waits_for_review_event(
    pg: _PostgresHarness,
) -> None:
    """H5 相同阻断事实停止轮询，人工复核仍能立即唤醒。"""

    case = await pg.seed_case(
        with_review=True,
        task_status="review",
        include_completed_exploration=True,
        target_platform="h5",
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )

    assert await repository.defer_unchanged_completion_gate(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    assert await pg.connection.fetchval(
        """SELECT finalization_retry_at = 'infinity'::timestamptz
           FROM qc_task WHERE id = $1::uuid""",
        case.task_id,
    )

    assert case.execution_id is not None
    assert await PostgresTaskInsightRepository(pool=pg.pool).update_human_verdict(
        execution_id=case.execution_id,
        verdict="pass",
        note="H5 复核事实变化",
        operator=case.user_id,
        task_id=case.task_id,
        access_scope=_scope(case),
    )
    await _assert_retry_due(pg, case.task_id)


@pytest.mark.asyncio
async def test_h5_review_status_change_cannot_reuse_running_gate_snapshot(
    pg: _PostgresHarness,
) -> None:
    """H5 报告转人工复核时，旧 Gate 快照不能挡住 running→review。"""

    case = await pg.seed_case(
        with_review=True,
        task_status="running",
        include_completed_exploration=True,
    )
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET target_platform = 'h5',
            task_config = jsonb_build_object('workflow_version', 'v2')
        WHERE id = $1::uuid
        """,
        case.task_id,
    )
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    first = await repository.evaluate_and_seal(
        command=_gate_command(
            case,
            await pg.task_row(case.task_id),
            execution_status=TaskExecutionStatus.COMPLETED,
        )
    )
    assert first.snapshot.gate_passed is False
    assert (await pg.task_row(case.task_id))["status"] == "running"
    await pg.connection.execute(
        """
        UPDATE qc_task
        SET finalization_retry_at = now() - interval '1 second',
            finalization_recovery_state = 'active'
        WHERE id = $1::uuid
        """,
        case.task_id,
    )

    running_row = await pg.task_row(case.task_id)
    review_command = _gate_command(case, running_row)
    assert await repository.defer_unchanged_completion_gate(command=review_command) is False
    reviewed = await repository.evaluate_and_seal(command=review_command)
    assert reviewed.snapshot.gate_passed is False
    assert reviewed.finalization.row_version > int(running_row["row_version"])


@pytest.mark.asyncio
async def test_review_policy_wakes_blocked_gate_in_the_decision_transaction(
    pg: _PostgresHarness,
) -> None:
    """Policy 决议不能只改变审核事实而留下未来重试时间。"""
    case = await pg.seed_case(with_review=True, include_completed_exploration=True)
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    blocked = await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    assert not blocked.snapshot.gate_passed
    before = await pg.task_row(case.task_id)
    await repository.record_review_policy_decision(command=RecordReviewPolicyDecisionCommand(
        task_id=case.task_id,
        decision_id=f"f01-policy-{case.task_id}",
        policy_hash="sha256:f01-reviewed-policy",
        satisfied=True,
        rationale="测试已批准的复核策略，不替代实际人工结论",
        expected_row_version=before["row_version"],
    ))
    await _assert_retry_due(pg, case.task_id)
    assert case.task_id in {
        item.task_id for item in await repository.list_recoverable_finalizations(limit=100)
    }
    assert not await repository.defer_unchanged_completion_gate(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )
    policy = await pg.connection.fetchrow(
        """SELECT decision.review_generation, decision.open_review_count, decision.satisfied
           FROM qc_review_policy_gate_decision AS decision
           JOIN qc_task AS task ON task.review_policy_decision_id = decision.decision_id
           WHERE task.id = $1::uuid""",
        case.task_id,
    )
    assert tuple(policy) == (before["review_generation"], 1, True)


@pytest.mark.asyncio
async def test_review_commit_after_unchanged_check_preserves_immediate_wakeup(
    pg: _PostgresHarness,
) -> None:
    """复查持有行锁期间到来的人工审核，在复查提交后仍能把时间提前。"""
    case = await pg.seed_case(with_review=True, include_completed_exploration=True)
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    await repository.evaluate_and_seal(
        command=_gate_command(case, await pg.task_row(case.task_id))
    )

    class HeldConnectionPool:
        @asynccontextmanager
        async def acquire(self):
            yield pg.connection

    review_task = None
    try:
        async with pg.connection.transaction():
            await pg.connection.fetchrow(
                "SELECT id FROM qc_task WHERE id = $1::uuid FOR UPDATE", case.task_id,
            )
            blocker_pid = pg.connection.get_server_pid()
            review_task = asyncio.create_task(
                PostgresWorkstationReviewRepository(pool=pg.pool).update_review_status(
                    review_id=case.review_id, status="approved", reason="并发复核",
                    access_scope=_scope(case),
                )
            )
            # 读取 PG 锁等待证明两条真实事务已发生竞争，而非依赖定时猜测。
            async with asyncio.timeout(3):
                while not await pg.pool.fetchval(
                    """SELECT EXISTS (
                           SELECT 1 FROM pg_stat_activity
                           WHERE $1::integer = ANY(pg_blocking_pids(pid))
                       )""",
                    blocker_pid,
                ):
                    await asyncio.sleep(0.01)
            assert not review_task.done()
            assert await PostgresTaskFinalizationRepository(
                pool=HeldConnectionPool(),
            ).defer_unchanged_completion_gate(
                command=_gate_command(case, await pg.task_row(case.task_id)),
            )
            deferred = await pg.task_row(case.task_id)
            assert deferred["finalization_waits_for_event"] is True
        assert await review_task is not None
        await _assert_retry_due(pg, case.task_id)
        assert not await repository.defer_unchanged_completion_gate(
            command=_gate_command(case, await pg.task_row(case.task_id)),
        )
    finally:
        if review_task is not None and not review_task.done():
            review_task.cancel()
            await asyncio.gather(review_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("open", "exploration_draining"))
@pytest.mark.parametrize("entrypoint", ("workstation", "insight", "policy"))
async def test_review_wakes_early_finalization_without_advancing_its_stage(
    pg: _PostgresHarness,
    stage: str,
    entrypoint: str,
) -> None:
    """审核也应唤醒尚在前置收口阶段的任务，不能被未来退避时间吞掉。"""
    case = await pg.seed_case(with_review=True, task_status="running", session_draining=False)
    repository = PostgresTaskFinalizationRepository(pool=pg.pool)
    await pg.connection.execute(
        """UPDATE qc_task SET finalization_state = $2::varchar,
               exploration_draining_at = CASE
                   WHEN $2::varchar = 'exploration_draining' THEN now() END,
               finalization_retry_at = now() + interval '1 hour'
           WHERE id = $1::uuid""",
        case.task_id, stage,
    )
    assert case.task_id not in {
        item.task_id for item in await repository.list_recoverable_finalizations(limit=100)
    }
    if entrypoint == "workstation":
        assert await PostgresWorkstationReviewRepository(pool=pg.pool).update_review_status(
            review_id=case.review_id, status="approved", reason="收口前置阶段复核",
            access_scope=_scope(case),
        )
    elif entrypoint == "insight":
        assert await PostgresTaskInsightRepository(pool=pg.pool).update_human_verdict(
            execution_id=case.execution_id, verdict="pass", note="收口前置阶段复核",
            operator=case.user_id, task_id=case.task_id, access_scope=_scope(case),
        )
    else:
        row = await pg.task_row(case.task_id)
        await repository.record_review_policy_decision(command=RecordReviewPolicyDecisionCommand(
            task_id=case.task_id, decision_id=f"f01-early-policy-{case.task_id}",
            policy_hash="sha256:f01-early-policy", satisfied=True,
            rationale="前置阶段的复核策略决议", expected_row_version=row["row_version"],
        ))
    await _assert_retry_due(pg, case.task_id)
    after = await pg.task_row(case.task_id)
    assert after["status"] == "running"
    assert after["finalization_state"] == stage
    assert case.task_id in {
        item.task_id for item in await repository.list_recoverable_finalizations(limit=100)
    }
