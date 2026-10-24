"""真实 PostgreSQL 上验证收口四维读模型只认当前收口轮次（09-11 审查 P1-08）。

完成检查以任务绑定的门禁快照为准、不拿别的轮次充数；补采请求按运行归属，不再按任务全量统计。
默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.tests.test_inspection_closure_settlement_postgres import (
    _seed_task,
    _seed_work_item,
)


async def test_closure_facts_follow_bound_gate_snapshot_and_current_run() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    old_run = str(uuid4())
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            """
            INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
            VALUES ($1::uuid, $2::uuid, 'main', 'Old Run', 'failed')
            """,
            old_run,
            ids["task"],
        )
        # 当前运行一条还在补采的请求，旧运行一条已终结的请求
        await _seed_work_item(
            connection,
            ids,
            name="current",
            page_seq=1,
            status="waiting_evidence",
            attempt_count=0,
            request_status="pending",
        )
        await _seed_work_item(
            connection,
            {**ids, "run": old_run},
            name="old",
            page_seq=2,
            status="waiting_evidence",
            attempt_count=0,
            request_status="unfulfillable",
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch = 1 WHERE id = $1::uuid", ids["task"]
        )
        # 上一轮有一份时间更晚的通过快照；本轮任务挂着的是等人工复核的未通过快照
        await _insert_gate(
            connection, ids["task"], epoch=0, sequence=1, passed=True, offset=timedelta(minutes=1)
        )
        bound = await _insert_gate(
            connection, ids["task"], epoch=1, sequence=2, passed=False, offset=timedelta(hours=-1)
        )
        await connection.execute(
            "UPDATE qc_task SET completion_gate_snapshot_id = $2 WHERE id = $1::uuid",
            ids["task"],
            bound,
        )
        repository = PostgresTaskInsightRepository(pool=pool)

        facts = await repository.fetch_closure_outcome_facts(
            task_id=ids["task"], agent_run_id=ids["run"]
        )

        assert facts["gate"]["gate_passed"] is False
        assert facts["gate"]["open_review_count"] == 1
        assert facts["supplement_request_counts"] == {"pending": 1}

        # 本轮后来又评估了一次，但任务仍挂着原快照：只认挂着的那份
        await _insert_gate(
            connection, ids["task"], epoch=1, sequence=3, passed=True, offset=timedelta(0)
        )
        facts = await repository.fetch_closure_outcome_facts(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert facts["gate"]["gate_passed"] is False

        # 刚通过门禁、报告还在绑定时任务不挂快照：取本轮最新一次，不回落到上一轮
        await connection.execute(
            "UPDATE qc_task SET completion_gate_snapshot_id = NULL WHERE id = $1::uuid",
            ids["task"],
        )
        facts = await repository.fetch_closure_outcome_facts(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert facts["gate"]["gate_passed"] is True

        # 本轮还没有任何快照时给空，不拿上一轮的结论充数
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch = 2 WHERE id = $1::uuid", ids["task"]
        )
        facts = await repository.fetch_closure_outcome_facts(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert facts["gate"] is None

        # 旧运行只看得到自己的请求
        facts = await repository.fetch_closure_outcome_facts(
            task_id=ids["task"], agent_run_id=old_run
        )
        assert facts["supplement_request_counts"] == {"unfulfillable": 1}
    finally:
        # 门禁快照是只追加的审计记录，测试清理要临时绕开保护触发器
        async with connection.transaction():
            await connection.execute("SET LOCAL session_replication_role = replica")
            await connection.execute(
                "DELETE FROM qc_completion_gate_snapshot WHERE task_id = $1::uuid", ids["task"]
            )
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _insert_gate(
    connection: asyncpg.Connection,
    task_id: str,
    *,
    epoch: int,
    sequence: int,
    passed: bool,
    offset: timedelta,
) -> str:
    snapshot_id = f"gate-{uuid4()}"
    await connection.execute(
        """
        INSERT INTO qc_completion_gate_snapshot (
            gate_snapshot_id, task_id, finalization_epoch, evaluation_sequence,
            protocol_version, facts_hash, exploration_terminal, no_pending_action,
            no_pending_capture_commit, no_nonterminal_enforced_work_item,
            no_active_session_request, candidate_rule_coverage_terminal,
            no_background_execution, review_policy_satisfied, pending_action_count,
            pending_capture_commit_count, nonterminal_enforced_work_item_count,
            active_session_request_count, candidate_rule_count,
            terminal_candidate_rule_count, background_execution_count,
            open_review_count, review_generation, block_reasons, gate_passed, evaluated_at
        ) VALUES (
            $1, $2::uuid, $3, $4,
            'task-completion-gate.v1', $7, TRUE, TRUE,
            TRUE, TRUE,
            TRUE, TRUE,
            TRUE, $5, 0,
            0, 0,
            0, 0,
            0, 0,
            CASE WHEN $5 THEN 0 ELSE 1 END, 0,
            CASE WHEN $5 THEN '[]'::jsonb ELSE '["review_policy_unsatisfied"]'::jsonb END,
            $5, now() + $6::interval
        )
        """,
        snapshot_id,
        task_id,
        epoch,
        sequence,
        passed,
        offset,
        f"facts-{snapshot_id}",
    )
    return snapshot_id
