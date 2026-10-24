"""真实 PostgreSQL 上验证 waiting_evidence 工作项的补采终态结算。

场景：同一条规则挂在两个页面的两个工作项上，两个补采请求都已不可补
（unfulfillable），其中一个工作项的尝试次数已经用尽。期望：
- 每个页面各自结算一条 evidence_insufficient 规则执行与一条复核项，不交叉相乘；
- 成员回写到本页面的执行记录；
- 尝试次数用尽的工作项照样收口为 partial，不会永远停在 waiting_evidence；
- 再次进入是幂等的。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)


async def test_terminal_requests_settle_per_capture_even_when_attempts_exhausted() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "capture_a",
            "capture_b",
            "work_a",
            "work_b",
            "attempt_a",
            "attempt_b",
        )
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task_with_two_pages(connection, ids)
        await _seed_waiting_work_item(
            connection,
            ids,
            work_key="work_a",
            capture_key="capture_a",
            attempt_key="attempt_a",
            attempt_count=3,
            max_attempts=3,
            request_id="req-a",
        )
        await _seed_waiting_work_item(
            connection,
            ids,
            work_key="work_b",
            capture_key="capture_b",
            attempt_key="attempt_b",
            attempt_count=1,
            max_attempts=3,
            request_id="req-b",
        )
        repository = PostgresInspectionWorkItemRepository(pool=pool)

        reopened = await repository.reopen_waiting_evidence(task_id=ids["task"])

        assert reopened == 0
        executions = await connection.fetch(
            """
            SELECT
                id::text AS id,
                page_capture_id::text AS page_capture_id,
                execution_status,
                source_idempotency_key,
                error_code,
                quality_outcome
            FROM qc_rule_execution
            WHERE task_id = $1::uuid
            """,
            ids["task"],
        )
        assert {
            (row["page_capture_id"], row["source_idempotency_key"]) for row in executions
        } == {
            (ids["capture_a"], "evidence-request-terminal:req-a"),
            (ids["capture_b"], "evidence-request-terminal:req-b"),
        }
        assert all(row["execution_status"] == "evidence_insufficient" for row in executions)
        assert all(row["error_code"] == "EVIDENCE_REQUEST_UNFULFILLABLE" for row in executions)
        assert all(row["quality_outcome"] == "evidence_gap" for row in executions)
        execution_by_capture = {row["page_capture_id"]: row["id"] for row in executions}

        review_items = await connection.fetch(
            """
            SELECT rule_execution_id::text AS rule_execution_id, status, trigger_reason
            FROM qc_review_item
            WHERE task_id = $1::uuid
            """,
            ids["task"],
        )
        assert sorted(row["rule_execution_id"] for row in review_items) == sorted(
            execution_by_capture.values()
        )
        assert all(row["status"] == "pending" for row in review_items)
        assert all(row["trigger_reason"] == "evidence_insufficient" for row in review_items)

        members = await connection.fetch(
            """
            SELECT
                member.work_item_id::text AS work_item_id,
                work_item.page_capture_id::text AS page_capture_id,
                member.status,
                member.rule_execution_id::text AS rule_execution_id,
                member.last_error_code
            FROM qc_inspection_work_item_rule AS member
            INNER JOIN qc_inspection_work_item AS work_item
                ON work_item.id = member.work_item_id
            WHERE work_item.task_id = $1::uuid
            """,
            ids["task"],
        )
        assert len(members) == 2
        for row in members:
            assert row["status"] == "evidence_insufficient"
            assert row["last_error_code"] == "EVIDENCE_REQUEST_TERMINAL"
            assert row["rule_execution_id"] == execution_by_capture[row["page_capture_id"]]

        work_items = await connection.fetch(
            """
            SELECT
                id::text AS id,
                status,
                attempt_count,
                max_attempts,
                finished_at,
                lease_owner
            FROM qc_inspection_work_item
            WHERE task_id = $1::uuid
            """,
            ids["task"],
        )
        by_id = {row["id"]: row for row in work_items}
        assert by_id[ids["work_a"]]["attempt_count"] == by_id[ids["work_a"]]["max_attempts"]
        for row in work_items:
            assert row["status"] == "partial"
            assert row["finished_at"] is not None
            assert row["lease_owner"] is None

        reopened_again = await repository.reopen_waiting_evidence(task_id=ids["task"])

        assert reopened_again == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_rule_execution WHERE task_id = $1::uuid",
                ids["task"],
            )
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_review_item WHERE task_id = $1::uuid",
                ids["task"],
            )
            == 2
        )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _seed_task_with_two_pages(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'Terminal Settlement')
        """,
        ids["tenant"],
        f"terminal-settlement-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'Terminal Settlement', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"terminal-settlement-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash,
            rule_count, source_type
        )
        VALUES (
            $1::uuid, $2, 'Terminal Settlement', 1, 'enabled',
            'snapshot-1', 1, 'manual'
        )
        """,
        ids["ruleset"],
        f"terminal-settlement-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_rule (
            id, ruleset_id, rule_code, name, category, executor_type,
            severity, status, check_point
        )
        VALUES (
            $1::uuid, $2::uuid, 'alioth_review_1', 'Review Rule',
            'visual', 'hybrid', 'medium', 'enabled', '检查页面'
        )
        """,
        ids["rule"],
        ids["ruleset"],
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, submitter_id, tenant_id
        )
        VALUES (
            $1::uuid, 'https://example.com', $2::uuid, 'running',
            $3::uuid, $4::uuid
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (
            id, task_id, agent_type, agent_name, status
        )
        VALUES ($1::uuid, $2::uuid, 'main', 'Review Main', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    for capture_key, page_seq, page_slug in (
        ("capture_a", 1, "home"),
        ("capture_b", 2, "video_list"),
    ):
        # qc_page_capture 的插入触发器会同步一条同 id 的 qc_capture 公共行
        await connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, page_seq, page_slug,
                url, page_type, capture_status
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5,
                'https://example.com', $5, 'captured'
            )
            """,
            ids[capture_key],
            ids["task"],
            ids["run"],
            page_seq,
            page_slug,
        )


async def _seed_waiting_work_item(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    work_key: str,
    capture_key: str,
    attempt_key: str,
    attempt_count: int,
    max_attempts: int,
    request_id: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item (
            id, task_id, page_capture_id, producer_agent_run_id,
            consumer_agent_run_id, ruleset_id, ruleset_version,
            ruleset_snapshot_hash, batch_key, strategy, category,
            session_dependency, input_hash, status, attempt_count,
            max_attempts, control_mode, finalization_epoch
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4::uuid,
            $4::uuid, $5::uuid, 1,
            'snapshot-1', 'hybrid:visual:snapshot', 'hybrid', 'visual',
            'snapshot', $6, 'waiting_evidence', $7,
            $8, 'enforced', 0
        )
        """,
        ids[work_key],
        ids["task"],
        ids[capture_key],
        ids["run"],
        ids["ruleset"],
        f"input-{work_key}",
        attempt_count,
        max_attempts,
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item_rule (
            work_item_id, rule_id, rule_code, ordinal, contract_hash, status
        )
        VALUES ($1::uuid, $2::uuid, 'alioth_review_1', 0, 'contract-1', 'pending')
        """,
        ids[work_key],
        ids["rule"],
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_attempt (
            id, work_item_id, consumer_agent_run_id, attempt_no,
            lease_token, status, input_hash, judged_at
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, 1,
            gen_random_uuid(), 'judged', $4, now()
        )
        """,
        ids[attempt_key],
        ids[work_key],
        ids["run"],
        f"input-{work_key}",
    )
    await connection.execute(
        """
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
            $4::uuid, $5::uuid, $6::uuid, $7::uuid, 'alioth_review_1',
            $8::uuid, 'snapshot', 'same_context_allowed',
            'logical_equivalent', 'read_only', 'B',
            '{}'::jsonb, 'unfulfillable', '目标页面已不可达', now() + interval '1 hour',
            now(), 0
        )
        """,
        request_id,
        f"requirement-{request_id}",
        f"payload-{request_id}",
        ids[work_key],
        ids[attempt_key],
        ids["task"],
        ids["rule"],
        ids[capture_key],
    )
