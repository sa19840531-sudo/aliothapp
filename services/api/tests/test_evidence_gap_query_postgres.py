"""在隔离 PG 验证证据缺口先限定任务后读取大结果包，保持历史与暂存语义。"""

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def gap_case():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    connection = await asyncpg.connect(
        require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    )
    try:
        for table in (
            "qc_task",
            "qc_rule",
            "qc_rule_execution",
            "qc_inspection_attempt",
            "qc_inspection_work_item",
            "qc_inspection_work_item_rule",
            "qc_page_graph_state",
            "qc_evidence_request",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA"
            )
        keys = {key: str(uuid4()) for key in ("task", "outside_task", "tenant", "actor", "run")}
        for task, tenant, owner in (
            (keys["task"], keys["tenant"], keys["actor"]),
            (keys["outside_task"], str(uuid4()), str(uuid4())),
        ):
            await connection.execute(
                "INSERT INTO qc_task(id,tenant_id,submitter_id) VALUES($1::uuid,$2::uuid,$3::uuid)",
                task,
                tenant,
                owner,
            )
        now = datetime.now(UTC)
        rule_ids = [str(uuid4()) for _ in range(4)]
        for index, rule in enumerate(rule_ids):
            await connection.execute(
                """INSERT INTO qc_rule(id,rule_code,name,category,severity,sort_order)
                   VALUES($1::uuid,$1::text,'缺口回归规则','visual','high',$2)""",
                rule,
                index,
            )
        selected_attempts = []
        captures = []
        for index, (task, run, rule) in enumerate(
            (
                (keys["task"], keys["run"], rule_ids[0]),
                (keys["task"], keys["run"], rule_ids[1]),
                (keys["task"], str(uuid4()), rule_ids[2]),
                (keys["outside_task"], keys["run"], rule_ids[3]),
            )
        ):
            work, attempt, capture = str(uuid4()), str(uuid4()), str(uuid4())
            selected_attempts.append(attempt)
            captures.append(capture)
            await connection.execute(
                """INSERT INTO qc_inspection_work_item(id,task_id,producer_agent_run_id,
                    consumer_agent_run_id,page_capture_id)
                   VALUES($1::uuid,$2::uuid,$3::uuid,$3::uuid,$4::uuid)""",
                work,
                task,
                run,
                capture,
            )
            await connection.execute(
                "INSERT INTO qc_inspection_work_item_rule(work_item_id,rule_id) "
                "VALUES($1::uuid,$2::uuid)",
                work,
                rule,
            )
            payload = {
                "deferred_decisions": [
                    {
                        "rule_id": rule,
                        "execution_status": "evidence_insufficient",
                        "judge_output": {"evidence_gap": {"missing": [f"证据{index}"]}},
                    }
                ],
                "unrelated_results": [str(uuid4()) for _ in range(1000)],
            }
            await connection.execute(
                """INSERT INTO qc_inspection_attempt(id,work_item_id,result_payload,created_at,
                                                     updated_at,judged_at,finished_at)
                   VALUES($1::uuid,$2::uuid,$3::jsonb,$4,$4,$4,$4)""",
                attempt,
                work,
                json.dumps(payload),
                now,
            )
        persisted = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_rule_execution(id,task_id,agent_run_id,rule_id,execution_status,
                    page_capture_id,judge_output,created_at,finished_at)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'evidence_insufficient',$5::uuid,
                      '{"evidence_gap":{"missing":["已落库证据"]}}',$6,$6)""",
            persisted,
            keys["task"],
            keys["run"],
            rule_ids[1],
            captures[1],
            now,
        )
        yield connection, keys, rule_ids, selected_attempts, persisted
    finally:
        await connection.close()


@pytest.mark.parametrize("current_run_only", [False, True])
async def test_evidence_gap_keeps_task_scope_run_filter_and_persisted_precedence(
    gap_case, current_run_only
):
    connection, keys, rules, attempts, persisted = gap_case
    scope = AccessScope(
        actor_user_id=keys["actor"], tenant_id=keys["tenant"], permissions=frozenset()
    )
    rows = await PostgresTaskInsightRepository(pool=_Pool(connection)).list_evidence_gap_rows(
        task_id=keys["task"],
        agent_run_id=keys["run"] if current_run_only else None,
        access_scope=scope,
    )
    expected = {persisted, f"deferred:{attempts[0]}:{rules[0]}"}
    if not current_run_only:
        expected.add(f"deferred:{attempts[2]}:{rules[2]}")
    assert {row["execution_id"] for row in rows} == expected
    assert all(isinstance(row["evidence_gap"], dict) for row in rows)
    assert next(row for row in rows if row["execution_id"] == persisted)["evidence_gap"] == {
        "missing": ["已落库证据"]
    }
    denied = AccessScope(
        actor_user_id=str(uuid4()), tenant_id=keys["tenant"], permissions=frozenset()
    )
    assert (
        await PostgresTaskInsightRepository(pool=_Pool(connection)).list_evidence_gap_rows(
            task_id=keys["task"],
            agent_run_id=None,
            access_scope=denied,
        )
        == []
    )
