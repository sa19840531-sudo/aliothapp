"""真实 PostgreSQL 上验证按质检规则统一的规则结果口径（2026-09-17，审查 #10 / 09-11 P0-04）。

H5/App 的判定结果写在独立判定 Run 下，探索主 Run 收口时给没判到的规则留占位。报告、任务详情与
收口判断都要取同一套结果：不分 Run，同一条规则已有真实结论时占位不再单独算。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.report_generation import ReportGenerationService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.queue.state_store import _requires_task_review
from services.api.tests.test_inspection_closure_settlement_postgres import _seed_task


async def test_occluded_placeholder_yields_only_to_conclusion_on_same_page_type() -> None:
    """弹窗遮挡时记的证据不足是占位：同页面类型判出真实结论才顶掉，别的页面不算。"""
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    occluded_home, clean_home, occluded_list = (str(uuid4()) for _ in range(3))
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        for page_id, page_seq, page_type in (
            (occluded_home, 1, "home"),
            (clean_home, 2, "home"),
            (occluded_list, 3, "video_list"),
        ):
            await connection.execute(
                """
                INSERT INTO qc_page_capture (
                    id, task_id, agent_run_id, page_seq, page_slug, url, page_type, capture_status
                )
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'https://example.com', $5, 'captured')
                """,
                page_id,
                ids["task"],
                ids["run"],
                page_seq,
                page_type,
            )
        occluded_output = {"presentation_rejection": {"reason_code": "BASE_PAGE_OCCLUDED"}}
        execution_ids = {}
        for name, page_id, status, output in (
            ("occluded_home", occluded_home, "evidence_insufficient", occluded_output),
            ("clean_home", clean_home, "pass", {}),
            ("occluded_list", occluded_list, "evidence_insufficient", occluded_output),
        ):
            execution_ids[name] = await connection.fetchval(
                """
                INSERT INTO qc_rule_execution (
                    task_id, rule_id, page_capture_id, agent_run_id, execution_status,
                    judge_strategy, judge_output
                )
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, 'hybrid', $6::jsonb)
                RETURNING id::text
                """,
                ids["task"],
                ids["rule"],
                page_id,
                ids["run"],
                status,
                json.dumps(output),
            )
        expected = {
            execution_ids["clean_home"]: "pass",
            execution_ids["occluded_list"]: "evidence_insufficient",
        }

        task_repository = PostgresTaskRepository(pool=pool)
        executions = await task_repository.list_rule_executions(ids["task"])
        insight_rows = await PostgresTaskInsightRepository(pool=pool).list_rule_execution_rows(
            task_id=ids["task"],
            agent_run_id=None,
        )

        assert {item.execution_id: item.execution_status for item in executions} == expected
        assert {row["execution_id"]: row["execution_status"] for row in insight_rows} == expected
        # 遮挡缺证保留诊断，不能通过人工确认补成业务结论。
        assert await _requires_task_review(connection, task_id=ids["task"]) is False
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute(
            "DELETE FROM qc_rule WHERE ruleset_id = $1::uuid", ids["ruleset"]
        )
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_rule_rulesets_map_rules_to_their_own_ruleset() -> None:
    """报告按「几期规则」分组：规则直接对到自己的规则集；不是 UUID 的编号查不到也不报错。"""
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)

        rulesets = await PostgresTaskInsightRepository(pool=pool).list_rule_rulesets(
            rule_ids=[ids["rule"], "snapshot-rule", str(uuid4())]
        )

        assert rulesets == {
            ids["rule"]: {
                "ruleset_code": f"closure-settlement-{ids['ruleset']}",
                "ruleset_name": "Closure Settlement",
            }
        }
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute(
            "DELETE FROM qc_rule WHERE ruleset_id = $1::uuid", ids["ruleset"]
        )
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_rule_results_are_unified_across_runs_and_placeholders_yield_to_conclusions() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    judged_rule_id, untouched_rule_id = str(uuid4()), str(uuid4())
    judge_run_id, page_id = str(uuid4()), str(uuid4())
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        for rule_id, rule_code, sort_order in (
            (judged_rule_id, "alioth_scope_judged", 2),
            (untouched_rule_id, "alioth_scope_untouched", 3),
        ):
            await connection.execute(
                """
                INSERT INTO qc_rule (
                    id, ruleset_id, rule_code, name, category, executor_type,
                    severity, status, check_point, sort_order
                )
                VALUES (
                    $1::uuid, $2::uuid, $3, $3, 'visual', 'hybrid',
                    'high', 'enabled', '检查页面', $4
                )
                """,
                rule_id,
                ids["ruleset"],
                rule_code,
                sort_order,
            )
        await connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, page_seq, page_slug, url, page_type, capture_status
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, 1, 'home', 'https://example.com', 'home', 'captured'
            )
            """,
            page_id,
            ids["task"],
            ids["run"],
        )
        # 独立判定 Run 比探索主 Run 晚建：以前「最近一个 Run」会被选成报告默认 Run
        await connection.execute(
            """
            INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status, created_at)
            VALUES ($1::uuid, $2::uuid, 'rule_judge', 'H5 Inspection Consumer', 'running',
                    now() + interval '1 minute')
            """,
            judge_run_id,
            ids["task"],
        )
        placeholder_ids = {}
        for rule_id, reason in (
            (judged_rule_id, "candidate_not_executed"),
            (untouched_rule_id, "not_matched_by_run"),
        ):
            placeholder_ids[rule_id] = await connection.fetchval(
                """
                INSERT INTO qc_rule_execution (
                    task_id, rule_id, page_capture_id, agent_run_id, execution_status,
                    judge_strategy, judge_output
                )
                VALUES ($1::uuid, $2::uuid, NULL, $3::uuid, 'skipped', 'hybrid',
                        jsonb_build_object('reason', $4::text))
                RETURNING id::text
                """,
                ids["task"],
                rule_id,
                ids["run"],
                reason,
            )
        judged_id = await connection.fetchval(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, agent_run_id, execution_status, judge_strategy
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'pass', 'hybrid')
            RETURNING id::text
            """,
            ids["task"],
            judged_rule_id,
            page_id,
            judge_run_id,
        )
        expected = {judged_id: "pass", placeholder_ids[untouched_rule_id]: "skipped"}

        task_repository = PostgresTaskRepository(pool=pool)
        executions = await task_repository.list_rule_executions(ids["task"])
        insight_rows = await PostgresTaskInsightRepository(pool=pool).list_rule_execution_rows(
            task_id=ids["task"],
            agent_run_id=None,
        )
        report = await ReportGenerationService(
            query_service=TaskQueryService(repository=task_repository)
        ).generate_task_report(ids["task"])

        assert {item.execution_id: item.execution_status for item in executions} == expected
        assert {row["execution_id"]: row["execution_status"] for row in insight_rows} == expected
        assert report["agent_run_id"] == ids["run"]
        assert (report["summary"]["total_rules"], report["summary"]["pass"]) == (2, 1)
        # 被顶掉的「候选没执行」占位本来要人工确认，现在不再挡住任务完成
        assert await _requires_task_review(connection, task_id=ids["task"]) is False

        await connection.execute(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, agent_run_id, execution_status, judge_strategy
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'evidence_insufficient', 'hybrid')
            """,
            ids["task"],
            untouched_rule_id,
            page_id,
            judge_run_id,
        )
        # 独立判定 Run 的缺证仍是诊断，真实业务转人工才进入待确认。
        assert await _requires_task_review(connection, task_id=ids["task"]) is False
        await connection.execute(
            """
            UPDATE qc_rule_execution SET execution_status = 'manual_review'
            WHERE task_id = $1::uuid AND rule_id = $2::uuid AND agent_run_id = $3::uuid
            """,
            ids["task"],
            untouched_rule_id,
            judge_run_id,
        )
        assert await _requires_task_review(connection, task_id=ids["task"]) is True
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute(
            "DELETE FROM qc_rule WHERE ruleset_id = $1::uuid", ids["ruleset"]
        )
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()
