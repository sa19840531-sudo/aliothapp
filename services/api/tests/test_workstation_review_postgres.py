"""连接级临时表验证人工审核资格和删除边界，不写业务表。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.workstation_reviews import WorkstationReviewListFilter
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_review_repository import (
    PostgresWorkstationReviewRepository,
)


class ConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def connection():
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离 ALIOTH_TEST_DATABASE_URL；测试只使用连接级临时表")
    database_url = require_isolated_test_database_url(
        database_url, os.getenv("ALIOTH_DATABASE_URL") or None
    )
    database = await asyncpg.connect(database_url)
    try:
        await database.execute(
            """
            CREATE TEMP TABLE qc_task (
                id uuid PRIMARY KEY, tenant_id uuid, submitter_id uuid, department_name text,
                task_config jsonb DEFAULT '{}'::jsonb, target_url text,
                target_platform text DEFAULT 'web',
                status varchar DEFAULT 'review', row_version bigint DEFAULT 0,
                deleted_at timestamptz, finalization_state varchar DEFAULT 'open',
                finalization_agent_run_id uuid, started_at timestamptz,
                locked_at timestamptz, finished_at timestamptz,
                updated_at timestamptz DEFAULT now(), last_error text
            );
            CREATE TEMP TABLE qc_rule (id uuid PRIMARY KEY, name text);
            CREATE TEMP TABLE qc_rule_execution (
                id uuid PRIMARY KEY, task_id uuid, rule_id uuid, agent_run_id uuid,
                page_capture_id uuid, execution_status text, result_summary text,
                judge_input jsonb DEFAULT '{}'::jsonb, judge_output jsonb DEFAULT '{}'::jsonb,
                human_verdict text, human_verdict_note text, human_verdict_by text,
                human_verdict_at timestamptz,
                review_status text, review_reason text, updated_at timestamptz DEFAULT now()
            );
            CREATE TEMP TABLE qc_review_item (
                id uuid PRIMARY KEY, rule_execution_id uuid, task_id uuid,
                trigger_reason text DEFAULT 'manual_flag', status text DEFAULT 'pending',
                reviewer uuid, review_reason text, priority integer DEFAULT 100,
                created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
                reviewed_at timestamptz
            );
            CREATE TEMP TABLE qc_page_capture (id uuid, page_type text);
            CREATE TEMP TABLE qc_capture (id uuid, agent_run_id uuid, metadata jsonb);
            CREATE TEMP TABLE qc_agent_run (
                id uuid, task_id uuid, agent_type text, created_at timestamptz
            );
            """
        )
        yield database
    finally:
        await database.close()


async def _seed(
    connection,
    *,
    execution_status="manual_review",
    judge_output=None,
    judge_input=None,
    deleted=False,
):
    ids = {key: str(uuid4()) for key in ("task", "rule", "execution", "review", "actor", "tenant")}
    await connection.execute(
        """
        INSERT INTO qc_task (id, submitter_id, tenant_id, task_config, deleted_at)
        VALUES ($1::uuid, $2::uuid, $3::uuid, '{"task_name":"审核资格测试"}',
                CASE WHEN $4::boolean THEN now() ELSE NULL END)
        """,
        ids["task"],
        ids["actor"],
        ids["tenant"],
        deleted,
    )
    await connection.execute(
        "INSERT INTO qc_rule (id, name) VALUES ($1::uuid, '测试规则')", ids["rule"]
    )
    await connection.execute(
        """
        INSERT INTO qc_rule_execution (
            id, task_id, rule_id, execution_status, judge_input, judge_output
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb, $6::jsonb)
        """,
        ids["execution"],
        ids["task"],
        ids["rule"],
        execution_status,
        json.dumps(judge_input or {}),
        json.dumps(judge_output or {}),
    )
    await connection.execute(
        "INSERT INTO qc_review_item (id, rule_execution_id, task_id) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid)",
        ids["review"],
        ids["execution"],
        ids["task"],
    )
    scope = AccessScope(
        actor_user_id=ids["actor"], tenant_id=ids["tenant"], permissions=frozenset()
    )
    return ids, scope, PostgresWorkstationReviewRepository(pool=ConnectionPool(connection))


@pytest.mark.parametrize(
    "case",
    [
        {"execution_status": "evidence_insufficient"},
        {"execution_status": "error"},
        {"judge_output": {"reason": "candidate_not_executed"}},
        {"judge_input": {"bootstrap": True}},
        {"judge_output": {"reason": "semantic_provider_http_500"}},
        {"judge_output": {"model_output": {"conflicts": ["platform_dependency_unavailable"]}}},
    ],
)
async def test_undecidable_review_cannot_enter_queue_or_receive_human_verdict(connection, case):
    ids, scope, repository = await _seed(connection, **case)
    assert (await repository.list_reviews(WorkstationReviewListFilter(), access_scope=scope))[
        "items"
    ] == []
    for status in ("approved", "rejected", "needs_recheck"):
        assert (
            await repository.update_review_status(
                review_id=ids["review"],
                status=status,
                reason="不能覆盖检测缺口",
                access_scope=scope,
            )
            is None
        )
    row = await connection.fetchrow(
        """
        SELECT review.status, execution.human_verdict FROM qc_review_item review
        JOIN qc_rule_execution execution ON execution.id = review.rule_execution_id
        WHERE review.id = $1::uuid
        """,
        ids["review"],
    )
    assert dict(row) == {"status": "pending", "human_verdict": None}


@pytest.mark.parametrize("status,verdict", [("approved", "pass"), ("rejected", "fail")])
async def test_real_business_manual_review_is_actionable_and_keeps_confirmed_history(
    connection, status, verdict
):
    ids, scope, repository = await _seed(connection, judge_output={"reason": "low_confidence"})
    listed = await repository.list_reviews(WorkstationReviewListFilter(), access_scope=scope)
    assert listed["total"] == 1
    assert listed["items"][0]["review_id"] == ids["review"]
    assert await repository.update_review_status(
        review_id=ids["review"], status=status, reason="依据页面确认", access_scope=scope
    ) == {"review_id": ids["review"], "status": status, "message": "审核已更新"}
    execution = await connection.fetchrow(
        "SELECT execution_status, human_verdict, human_verdict_by, human_verdict_at "
        "FROM qc_rule_execution WHERE id = $1::uuid",
        ids["execution"],
    )
    assert execution["execution_status"] == "manual_review"
    assert execution["human_verdict"] == verdict
    assert execution["human_verdict_by"] == ids["actor"]
    assert execution["human_verdict_at"] is not None
    assert (
        await connection.fetchval("SELECT status FROM qc_task WHERE id = $1::uuid", ids["task"])
        == "done"
    )
    history = await repository.list_reviews(
        WorkstationReviewListFilter(status=status), access_scope=scope
    )
    assert history["items"][0]["review_id"] == ids["review"]


async def test_review_rejudgment_replaces_old_actor_and_recheck_clears_confirmation(connection):
    ids, scope, repository = await _seed(connection)
    old_actor = str(uuid4())
    await connection.execute(
        "UPDATE qc_rule_execution SET human_verdict='pass', human_verdict_by=$2, "
        "human_verdict_at=now() - interval '1 day' WHERE id=$1::uuid",
        ids["execution"], old_actor,
    )
    assert await repository.update_review_status(
        review_id=ids["review"], status="rejected", reason="复核后不通过", access_scope=scope,
    )
    row = await connection.fetchrow(
        "SELECT human_verdict, human_verdict_by, human_verdict_at FROM qc_rule_execution "
        "WHERE id=$1::uuid", ids["execution"],
    )
    assert row["human_verdict"] == "fail"
    assert row["human_verdict_by"] == ids["actor"] != old_actor
    assert row["human_verdict_at"] is not None
    assert await repository.update_review_status(
        review_id=ids["review"], status="needs_recheck", reason="补材料复查", access_scope=scope,
    )
    row = await connection.fetchrow(
        "SELECT human_verdict, human_verdict_by, human_verdict_at FROM qc_rule_execution "
        "WHERE id=$1::uuid", ids["execution"],
    )
    assert dict(row) == {
        "human_verdict": None, "human_verdict_by": None, "human_verdict_at": None,
    }


async def test_deleted_task_review_is_hidden_and_rejects_direct_mutation(connection):
    ids, scope, repository = await _seed(connection, deleted=True)
    assert (await repository.list_reviews(WorkstationReviewListFilter(), access_scope=scope))[
        "total"
    ] == 0
    assert (
        await repository.update_review_status(
            review_id=ids["review"],
            status="approved",
            reason="删除后不能继续审核",
            access_scope=scope,
        )
        is None
    )
    assert (
        await connection.fetchval(
            "SELECT status FROM qc_review_item WHERE id = $1::uuid", ids["review"]
        )
        == "pending"
    )
    assert (
        await connection.fetchval(
            "SELECT human_verdict FROM qc_rule_execution WHERE id = $1::uuid", ids["execution"]
        )
        is None
    )
