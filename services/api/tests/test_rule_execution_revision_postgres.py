"""真实 PostgreSQL 上验证规则结论被改写前先留底（09-17 审查复核，P1-10 遗留）。

规则执行按「任务 + 规则 + 页面采集」只存一行，补采后重判会整行改写；改写前的结论要按顺序追加进
结论历史，复核、人工结论这类不改结论的更新不留底，历史行本身不能改写。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.tests.test_inspection_closure_settlement_postgres import _seed_task


async def test_rejudge_keeps_overwritten_conclusions_in_order() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    connection = await asyncpg.connect(database_url)
    try:
        await _seed_task(connection, ids)
        execution_id = await connection.fetchval(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, agent_run_id, execution_status, judge_strategy,
                result_summary, evidence_artifact_ids
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, 'evidence_insufficient', 'hybrid',
                '弹窗挡住了页面', '["artifact-before"]'::jsonb
            )
            RETURNING id::text
            """,
            ids["task"],
            ids["rule"],
            ids["run"],
        )

        # 复核与人工结论不改结论，不留底
        await connection.execute(
            """
            UPDATE qc_rule_execution
            SET review_status = 'pending', review_reason = '待确认', human_verdict = 'pass'
            WHERE id = $1::uuid
            """,
            execution_id,
        )
        assert await _revisions(connection, execution_id) == []

        # 补采后重判，随后再改判一次
        for status, summary, artifact_id in (
            ("fail", "页面缺少提示文案", "artifact-after"),
            ("pass", "补证后确认有提示文案", "artifact-final"),
        ):
            await connection.execute(
                """
                UPDATE qc_rule_execution
                SET execution_status = $2,
                    result_summary = $3,
                    evidence_artifact_ids = jsonb_build_array($4::text)
                WHERE id = $1::uuid
                """,
                execution_id,
                status,
                summary,
                artifact_id,
            )

        assert await _revisions(connection, execution_id) == [
            (1, "evidence_insufficient", "弹窗挡住了页面", ["artifact-before"], "pending", "pass"),
            (2, "fail", "页面缺少提示文案", ["artifact-after"], "pending", "pass"),
        ]
        current = await connection.fetchrow(
            "SELECT execution_status, result_summary FROM qc_rule_execution WHERE id = $1::uuid",
            execution_id,
        )
        assert (current["execution_status"], current["result_summary"]) == (
            "pass",
            "补证后确认有提示文案",
        )
        with pytest.raises(asyncpg.exceptions.RaiseError, match="规则结论历史不可改写"):
            await connection.execute(
                """
                UPDATE qc_rule_execution_revision
                SET result_summary = '改掉旧结论'
                WHERE rule_execution_id = $1::uuid
                """,
                execution_id,
            )
    finally:
        # 删任务时结论历史随规则执行一起级联删除，不能被只追加约束挡住
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await connection.close()


async def _revisions(
    connection: asyncpg.Connection,
    execution_id: str,
) -> list[tuple[int, str, str | None, list[str], str | None, str | None]]:
    rows = await connection.fetch(
        """
        SELECT revision_no, execution_status, result_summary, evidence_artifact_ids::text,
               review_status, human_verdict
        FROM qc_rule_execution_revision
        WHERE rule_execution_id = $1::uuid
        ORDER BY revision_no
        """,
        execution_id,
    )
    return [
        (
            row["revision_no"],
            row["execution_status"],
            row["result_summary"],
            json.loads(row["evidence_artifact_ids"]),
            row["review_status"],
            row["human_verdict"],
        )
        for row in rows
    ]
