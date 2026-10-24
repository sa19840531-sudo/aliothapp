"""真实 PostgreSQL 上验证收口结算：只结算再也推进不了的工作项，还能推进的一个不碰。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

_DEFAULT_CONSUMER_RUN_ID = object()
_DEFAULT_SESSION_OWNER_RUN_ID = object()


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )


async def test_settlement_closes_only_work_that_cannot_progress() -> None:
    database_url = _test_database_url()

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        # 六种工作项：前四种再也推进不了，后两种还能自己往前走
        cases = {
            "no_request": {"status": "waiting_evidence", "attempts": 0, "request": None},
            "satisfied_exhausted": {
                "status": "waiting_evidence",
                "attempts": 3,
                "request": "satisfied",
            },
            "unfulfillable": {
                "status": "waiting_evidence",
                "attempts": 1,
                "request": "unfulfillable",
            },
            "retry_exhausted": {"status": "retry_wait", "attempts": 3, "request": None},
            "satisfied_retryable": {
                "status": "waiting_evidence",
                "attempts": 1,
                "request": "satisfied",
            },
            "inflight": {"status": "waiting_evidence", "attempts": 3, "request": "pending"},
        }
        work_ids: dict[str, str] = {}
        capture_ids: dict[str, str] = {}
        for index, (name, spec) in enumerate(cases.items(), start=1):
            work_ids[name], capture_ids[name] = await _seed_work_item(
                connection,
                ids,
                name=name,
                page_seq=index,
                status=str(spec["status"]),
                attempt_count=int(spec["attempts"]),
                request_status=spec["request"],
            )
        repository = PostgresInspectionWorkItemRepository(pool=pool)

        result = await repository.settle_stalled_work_for_closure(task_id=ids["task"])

        assert result.settled_work_item_count == 4
        assert result.settled_rule_count == 4
        assert result.reason_counts == {
            "no_supplement_request": 1,
            "supplement_satisfied_but_retry_exhausted": 1,
            "supplement_unfulfillable": 1,
            "retry_budget_exhausted": 1,
        }

        statuses = {
            name: await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id = $1::uuid", work_id
            )
            for name, work_id in work_ids.items()
        }
        assert statuses["no_request"] == "partial"
        assert statuses["satisfied_exhausted"] == "partial"
        assert statuses["unfulfillable"] == "partial"
        assert statuses["retry_exhausted"] == "partial"
        # 还能重开或还在补采的一个都不碰
        assert statuses["satisfied_retryable"] == "waiting_evidence"
        assert statuses["inflight"] == "waiting_evidence"

        executions = await connection.fetch(
            """
            SELECT
                page_capture_id::text AS page_capture_id,
                execution_status,
                error_code,
                problem_desc,
                quality_outcome
            FROM qc_rule_execution
            WHERE task_id = $1::uuid
            """,
            ids["task"],
        )
        assert len(executions) == 4
        assert {row["execution_status"] for row in executions} == {"evidence_insufficient"}
        assert {row["error_code"] for row in executions} == {"INSPECTION_CLOSURE_SETTLEMENT"}
        assert {row["quality_outcome"] for row in executions} == {"evidence_gap"}
        reasons = {
            row["page_capture_id"]: row["problem_desc"] for row in executions
        }
        # 放弃原因逐类落库，运营看得出为什么没判
        assert "没有可执行的补采请求" in reasons[capture_ids["no_request"]]
        assert "补采证据已获得但重试次数用尽" in reasons[capture_ids["satisfied_exhausted"]]
        assert "补采请求已终结" in reasons[capture_ids["unfulfillable"]]
        assert "重试次数已用尽" in reasons[capture_ids["retry_exhausted"]]

        review_count = await connection.fetchval(
            "SELECT count(*) FROM qc_review_item WHERE task_id = $1::uuid", ids["task"]
        )
        assert review_count == 4

        members = await connection.fetch(
            """
            SELECT
                member.work_item_id::text AS work_item_id,
                member.status,
                member.last_error_code
            FROM qc_inspection_work_item_rule AS member
            INNER JOIN qc_inspection_work_item AS work_item ON work_item.id = member.work_item_id
            WHERE work_item.task_id = $1::uuid
            """,
            ids["task"],
        )
        settled_ids = {
            work_ids[name]
            for name in ("no_request", "satisfied_exhausted", "unfulfillable", "retry_exhausted")
        }
        for row in members:
            if row["work_item_id"] in settled_ids:
                assert row["status"] == "evidence_insufficient"
                assert row["last_error_code"] == "INSPECTION_CLOSURE_SETTLEMENT"
            else:
                assert row["status"] == "pending"

        # 再跑一次是幂等的：没有新的可结算工作项，也不会重复写执行或复核项
        again = await repository.settle_stalled_work_for_closure(task_id=ids["task"])
        assert again.settled_work_item_count == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_rule_execution WHERE task_id = $1::uuid", ids["task"]
            )
            == 4
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_review_item WHERE task_id = $1::uuid", ids["task"]
            )
            == 4
        )

        # 浏览器会话要关：本 Run 的活会话工作项即使还能重试也一并结算
        active_id, active_capture = await _seed_work_item(
            connection,
            ids,
            name="active",
            page_seq=7,
            status="pending",
            attempt_count=0,
            request_status=None,
            session_dependency="active_session",
        )
        closing = await repository.settle_stalled_work_for_closure(
            task_id=ids["task"], active_session_agent_run_id=ids["run"]
        )
        assert closing.reason_counts == {"active_session_closing": 1}
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id = $1::uuid", active_id
            )
            == "partial"
        )
        assert (
            await connection.fetchval(
                """
                SELECT problem_desc FROM qc_rule_execution
                WHERE task_id = $1::uuid AND page_capture_id = $2::uuid
                """,
                ids["task"],
                active_capture,
            )
        ).startswith("浏览器会话即将关闭")
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_active_session_settlement_matches_producer_owner_and_epoch() -> None:
    """活会话结算按生产 Run、会话属主和当前收口轮次精确匹配。"""

    database_url = _test_database_url()

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    other_run = str(uuid4())
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            """
            INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
            VALUES ($1::uuid, $2::uuid, 'main', 'Other Closure Run', 'running')
            """,
            other_run,
            ids["task"],
        )

        stale_id, _ = await _seed_work_item(
            connection,
            ids,
            name="active-stale-epoch",
            page_seq=1,
            status="pending",
            attempt_count=0,
            request_status=None,
            session_dependency="active_session",
            producer_run_id=ids["run"],
            session_owner_run_id=ids["run"],
            consumer_run_id=None,
            finalization_epoch=0,
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch = 1 WHERE id = $1::uuid",
            ids["task"],
        )

        cases = {
            # 未领取的工作项 consumer 为空，也必须能按所属活会话结算。
            "unclaimed": {
                "producer_run_id": ids["run"],
                "session_owner_run_id": ids["run"],
                "consumer_run_id": None,
                "finalization_epoch": 1,
            },
            # 已领取的工作项保留 consumer 审计身份，同时仍按 producer/owner 命中。
            "claimed": {
                "producer_run_id": ids["run"],
                "session_owner_run_id": ids["run"],
                "consumer_run_id": other_run,
                "finalization_epoch": 1,
            },
            "wrong_producer": {
                "producer_run_id": other_run,
                "session_owner_run_id": ids["run"],
                "consumer_run_id": None,
                "finalization_epoch": 1,
            },
            "wrong_owner": {
                "producer_run_id": ids["run"],
                "session_owner_run_id": other_run,
                "consumer_run_id": None,
                "finalization_epoch": 1,
            },
        }
        work_ids: dict[str, str] = {"stale_epoch": stale_id}
        for page_seq, (name, spec) in enumerate(cases.items(), start=2):
            work_id, _ = await _seed_work_item(
                connection,
                ids,
                name=f"active-{name}",
                page_seq=page_seq,
                status="pending",
                attempt_count=0,
                request_status=None,
                session_dependency="active_session",
                producer_run_id=str(spec["producer_run_id"]),
                session_owner_run_id=str(spec["session_owner_run_id"]),
                consumer_run_id=spec["consumer_run_id"],
                finalization_epoch=int(spec["finalization_epoch"]),
            )
            work_ids[name] = work_id

        repository = PostgresInspectionWorkItemRepository(pool=pool)
        result = await repository.settle_stalled_work_for_closure(
            task_id=ids["task"], active_session_agent_run_id=ids["run"]
        )

        assert result.settled_work_item_count == 2
        assert result.settled_rule_count == 2
        assert result.reason_counts == {"active_session_closing": 2}

        rows = await connection.fetch(
            """
            SELECT
                work_item.id::text AS work_item_id,
                work_item.status,
                execution.agent_run_id::text AS execution_agent_run_id
            FROM qc_inspection_work_item AS work_item
            LEFT JOIN qc_rule_execution AS execution
                ON execution.task_id = work_item.task_id
               AND execution.page_capture_id = work_item.page_capture_id
            WHERE work_item.task_id = $1::uuid
            """,
            ids["task"],
        )
        states = {
            next(name for name, work_id in work_ids.items() if work_id == row["work_item_id"]): row
            for row in rows
        }
        assert states["unclaimed"]["status"] == "partial"
        assert states["unclaimed"]["execution_agent_run_id"] == ids["run"]
        assert states["claimed"]["status"] == "partial"
        assert states["claimed"]["execution_agent_run_id"] == other_run
        for name in ("wrong_producer", "wrong_owner", "stale_epoch"):
            assert states[name]["status"] == "pending"
            assert states[name]["execution_agent_run_id"] is None
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _seed_task(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'Closure Settlement')
        """,
        ids["tenant"],
        f"closure-settlement-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $2, 'Closure Settlement', 'not-a-login-secret', '平台管理员', $3::uuid)
        """,
        ids["user"],
        f"closure-settlement-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Closure Settlement', 1, 'enabled', 'snapshot-1', 1, 'manual')
        """,
        ids["ruleset"],
        f"closure-settlement-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_rule (
            id, ruleset_id, rule_code, name, category, executor_type,
            severity, status, check_point
        )
        VALUES (
            $1::uuid, $2::uuid, 'alioth_closure_1', 'Closure Rule',
            'visual', 'hybrid', 'high', 'enabled', '检查页面'
        )
        """,
        ids["rule"],
        ids["ruleset"],
    )
    # 收口中：生命周期约束要求 exploration_draining_at 与 session_draining_at 都已落时间
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, submitter_id, tenant_id,
            finalization_state, exploration_draining_at, session_draining_at
        )
        VALUES (
            $1::uuid, 'https://example.com', $2::uuid, 'running', $3::uuid, $4::uuid,
            'session_draining', now(), now()
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Closure Settlement', 'running')
        """,
        ids["run"],
        ids["task"],
    )


async def _seed_work_item(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    name: str,
    page_seq: int,
    status: str,
    attempt_count: int,
    request_status: str | None,
    session_dependency: str = "snapshot",
    producer_run_id: str | None = None,
    session_owner_run_id: str | None | object = _DEFAULT_SESSION_OWNER_RUN_ID,
    consumer_run_id: str | None | object = _DEFAULT_CONSUMER_RUN_ID,
    finalization_epoch: int = 0,
) -> tuple[str, str]:
    capture_id = str(uuid4())
    work_id = str(uuid4())
    attempt_id = str(uuid4())
    producer_id = producer_run_id or ids["run"]
    if session_owner_run_id is _DEFAULT_SESSION_OWNER_RUN_ID:
        owner_id = producer_id if session_dependency == "active_session" else None
    else:
        owner_id = session_owner_run_id
    consumer_id = ids["run"] if consumer_run_id is _DEFAULT_CONSUMER_RUN_ID else consumer_run_id
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, page_type, capture_status
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'https://example.com', 'home', 'captured')
        """,
        capture_id,
        ids["task"],
        producer_id,
        page_seq,
        name,
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item (
            id, task_id, page_capture_id, producer_agent_run_id, consumer_agent_run_id,
            session_owner_agent_run_id, ruleset_id, ruleset_version, ruleset_snapshot_hash,
            batch_key, strategy, category, session_dependency, input_hash, status,
            attempt_count, max_attempts, control_mode, finalization_epoch
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6::uuid,
            $7::uuid, 1, 'snapshot-1',
            'hybrid:visual:' || $11, 'hybrid', 'visual', $11, $8, $9,
            $10, 3, 'enforced', $12
        )
        """,
        work_id,
        ids["task"],
        capture_id,
        producer_id,
        consumer_id,
        owner_id,
        ids["ruleset"],
        f"input-{name}",
        status,
        attempt_count,
        session_dependency,
        finalization_epoch,
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item_rule (
            work_item_id, rule_id, rule_code, ordinal, contract_hash, status
        )
        VALUES ($1::uuid, $2::uuid, 'alioth_closure_1', 0, 'contract-1', 'pending')
        """,
        work_id,
        ids["rule"],
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_attempt (
            id, work_item_id, consumer_agent_run_id, attempt_no,
            lease_token, status, input_hash
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, 1, gen_random_uuid(), 'running', $4)
        """,
        attempt_id,
        work_id,
        consumer_id,
        f"input-{name}",
    )
    if request_status is not None:
        terminal = request_status in {"satisfied", "unfulfillable", "expired", "cancelled"}
        await connection.execute(
            f"""
            INSERT INTO qc_evidence_request (
                request_id, requirement_hash, payload_hash, protocol_version,
                work_item_id, attempt_id, task_id, rule_id, rule_code,
                target_capture_id, session_dependency, replay_policy,
                required_equivalence, side_effect_policy, minimum_source_grade,
                request_payload, status, resolution_reason, expires_at,
                completed_at, finalization_epoch
            )
            VALUES (
                $1, $2, $3, 'evidence-request.v1',
                $4::uuid, $5::uuid, $6::uuid, $7::uuid, 'alioth_closure_1',
                $8::uuid, 'snapshot', 'same_context_allowed',
                'logical_equivalent', 'read_only', 'B',
                '{{}}'::jsonb, $9, '测试用请求', now() + interval '1 hour',
                {"now()" if terminal else "NULL"}, 0
            )
            """,
            f"request-{name}",
            f"requirement-{name}",
            f"payload-{name}",
            work_id,
            attempt_id,
            ids["task"],
            ids["rule"],
            capture_id,
            request_status,
        )
    return work_id, capture_id
