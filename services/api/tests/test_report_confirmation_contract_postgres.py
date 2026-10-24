"""用只读 PostgreSQL 表达式对账报告确认资格与 Python 判读，禁止检测缺口人工补成完成。"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.task_progress import (
    rule_display_status,
    rule_requires_human_confirmation,
)
from services.api.app.infrastructure.db.rule_confirmation_sql import requires_human_confirmation_sql
from services.api.app.infrastructure.db.task_insight_repository import (
    _EXECUTION_REASON_SQL,
    PostgresTaskInsightRepository,
)


async def test_confirmation_python_and_sql_match_on_real_postgres() -> None:
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式隔离的 ALIOTH_TEST_DATABASE_URL，本测试仅执行 SELECT")
    cases = [
        ("manual_review", {}, None, False, True),
        ("manual_review", {}, None, True, False),
        ("pass", {}, None, False, False),
        ("fail", {}, None, False, False),
        ("skipped", {}, None, False, False),
        ("evidence_insufficient", {}, None, False, False),
        ("error", {}, None, False, False),
        ("pending", {}, None, False, False),
        (None, {}, None, False, False),
        ("manual_review", {}, "模型 API 返回 HTTP 500，未生成结论", False, False),
    ]
    for code in (
        "semantic_provider_http_500",
        "semantic_provider_disconnect",
        "prompt_input_token_budget",
        "semantic_batch_single_failed",
        "semantic_judge_unavailable",
        "candidate_not_executed",
        "run_budget_exhausted",
    ):
        for normalized in (code, f"  {code.upper()}  "):
            cases.append(("manual_review", {"reason": normalized}, None, False, False))
            if code not in {"candidate_not_executed", "run_budget_exhausted"}:
                cases.append(
                    (
                        "manual_review",
                        {"reason": "business_review", "model_output": {"conflicts": [normalized]}},
                        None,
                        False,
                        False,
                    )
                )
    connection = await asyncpg.connect(url)
    try:
        for status, output, summary, bootstrap, expected in cases:
            row = await connection.fetchrow(
                f"""
                WITH execution AS (
                    SELECT $1::text AS execution_status, $2::jsonb AS judge_output,
                           $3::jsonb AS judge_input, $4::text AS result_summary,
                           NULL::text AS error_code
                ), capture AS (SELECT NULL::text AS inspection_failure_code),
                common_capture AS (SELECT '{{}}'::jsonb AS metadata)
                SELECT {requires_human_confirmation_sql("execution")} AS confirmable,
                       {_EXECUTION_REASON_SQL} AS execution_reason
                FROM execution, capture, common_capture
                """,
                status,
                json.dumps(output),
                json.dumps({"bootstrap": bootstrap}),
                summary,
            )
            display = rule_display_status(status, row["execution_reason"], is_bootstrap=bootstrap)
            actual = not bootstrap and rule_requires_human_confirmation(
                display, row["execution_reason"], result_summary=summary
            )
            assert bool(row["confirmable"]) == actual == expected, (status, output, summary)
    finally:
        await connection.close()


@pytest.mark.parametrize("source", ["column", "output", "output_with_other_column"])
@pytest.mark.parametrize(
    "code", ["SUPPLEMENTAL_EVIDENCE_DISABLED", "SUPPLEMENTAL_EVIDENCE_NOT_READY"]
)
async def test_supplemental_denial_reason_matches_completion_contract(
    source: str, code: str,
) -> None:
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式隔离的 ALIOTH_TEST_DATABASE_URL，本测试仅执行 SELECT")
    connection = await asyncpg.connect(url)
    try:
        output = {"reason": "product_not_applicable"}
        if source != "column":
            output["error_code"] = code
        column_code = code if source == "column" else (
            "UNRELATED_CODE" if source == "output_with_other_column" else None
        )
        reason = await connection.fetchval(
            f"""
            WITH execution AS (
                SELECT 'skipped'::text AS execution_status, $1::jsonb AS judge_output,
                       $2::text AS error_code
            ), capture AS (SELECT NULL::text AS inspection_failure_code),
            common_capture AS (SELECT '{{}}'::jsonb AS metadata)
            SELECT {_EXECUTION_REASON_SQL} FROM execution, capture, common_capture
            """,
            json.dumps(output), column_code,
        )
        assert reason == code.lower()
        assert rule_display_status("skipped", reason) == "evidence_insufficient"
    finally:
        await connection.close()


async def test_human_verdict_waits_for_task_deletion_and_rechecks_visibility() -> None:
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式隔离的 ALIOTH_TEST_DATABASE_URL")
    deletion = await asyncpg.connect(url)
    confirmation = await asyncpg.connect(url)
    schema = f"report_guard_{uuid4().hex}"
    pending = None
    try:
        # 两个连接共用独立测试 schema，模拟删除先持有任务锁；不访问正式任务。
        await deletion.execute(f'CREATE SCHEMA "{schema}"')
        for connection in (deletion, confirmation):
            await connection.execute(f'SET search_path TO "{schema}"')
        await deletion.execute("""
            CREATE TABLE qc_task (id uuid PRIMARY KEY, status text, deleted_at timestamptz);
            CREATE TABLE qc_rule_execution (id uuid PRIMARY KEY, task_id uuid);
        """)
        task_id, execution_id = str(uuid4()), str(uuid4())
        await deletion.execute("INSERT INTO qc_task VALUES ($1::uuid,'done',NULL)", task_id)
        await deletion.execute(
            "INSERT INTO qc_rule_execution VALUES ($1::uuid,$2::uuid)", execution_id, task_id
        )
        transaction = deletion.transaction()
        await transaction.start()
        await deletion.fetchval("SELECT status FROM qc_task WHERE id=$1::uuid FOR UPDATE", task_id)

        class SingleConnectionPool:
            @asynccontextmanager
            async def acquire(self):
                yield confirmation

        repository = PostgresTaskInsightRepository(pool=SingleConnectionPool())
        pending = asyncio.create_task(
            repository.update_human_verdict(
                execution_id=execution_id,
                verdict="pass",
                note=None,
                operator=None,
            )
        )
        for _ in range(100):
            waiting = await deletion.fetchval(
                "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",
                confirmation.get_server_pid(),
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "人工确认必须等待删除事务持有的任务锁"
        await deletion.execute("UPDATE qc_task SET deleted_at=now() WHERE id=$1::uuid", task_id)
        await transaction.commit()
        assert await asyncio.wait_for(pending, timeout=3) is None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if deletion.is_in_transaction():
            await deletion.execute("ROLLBACK")
        await confirmation.close()
        await deletion.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await deletion.close()


async def test_frozen_reason_read_requires_matching_task_run_status_and_execution_version() -> None:
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式隔离的 ALIOTH_TEST_DATABASE_URL")
    connection = await asyncpg.connect(url)
    try:
        # 临时表只存在于本连接，用最小事实执行真实仓储查询，不修改任何正式数据。
        await connection.execute("""
            CREATE TEMP TABLE qc_task (id uuid, status text DEFAULT 'done', deleted_at timestamptz);
            CREATE TEMP TABLE qc_review_item (rule_execution_id uuid);
            CREATE TEMP TABLE qc_capture (id uuid, metadata jsonb);
            CREATE TEMP TABLE qc_page_capture (id uuid, inspection_failure_code text);
            CREATE TEMP TABLE qc_rule_execution (
                id uuid, task_id uuid, agent_run_id uuid, execution_status text,
                judge_output jsonb, page_capture_id uuid,
                started_at timestamptz, finished_at timestamptz,
                judge_input jsonb DEFAULT '{}'::jsonb, result_summary text, human_verdict text,
                human_verdict_note text, human_verdict_by uuid, human_verdict_at timestamptz,
                    review_status text, review_reason text, updated_at timestamptz,
                    error_code text
            );
        """)
        task_id, run_id, execution_id = [str(uuid4()) for _ in range(3)]
        now = datetime.now(UTC)
        await connection.execute("INSERT INTO qc_task(id) VALUES ($1::uuid)", task_id)
        await connection.execute(
            """
            INSERT INTO qc_rule_execution (
                id,task_id,agent_run_id,execution_status,judge_output,started_at,finished_at
            ) VALUES ($1::uuid,$2::uuid,$3::uuid,'skipped',$4::jsonb,$5,$5)
            """,
            execution_id,
            task_id,
            run_id,
            json.dumps({"reason": "candidate_not_executed"}),
            now,
        )

        class SingleConnectionPool:
            @asynccontextmanager
            async def acquire(self):
                yield connection

        repository = PostgresTaskInsightRepository(pool=SingleConnectionPool())
        frozen = {
            "execution_id": execution_id,
            "execution_status": "skipped",
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
        }
        assert await repository.list_rule_execution_reasons(
            task_id=task_id, agent_run_id=run_id, executions=[frozen]
        ) == {execution_id: "candidate_not_executed"}
        for changed in (
            {"execution_id": str(uuid4())},
            {"execution_status": "manual_review"},
            {"started_at": (now + timedelta(seconds=1)).isoformat()},
            {"finished_at": (now + timedelta(seconds=1)).isoformat()},
        ):
            assert (
                await repository.list_rule_execution_reasons(
                    task_id=task_id, agent_run_id=run_id, executions=[{**frozen, **changed}]
                )
                == {}
            )
        assert (
            await repository.list_rule_execution_reasons(
                task_id=str(uuid4()), agent_run_id=run_id, executions=[frozen]
            )
            == {}
        )
        assert (
            await repository.list_rule_execution_reasons(
                task_id=task_id, agent_run_id=str(uuid4()), executions=[frozen]
            )
            == {}
        )
        # 写入时再次检查资格，覆盖读取后状态变更和任务被删除的竞态。
        for status in ("skipped", "error", "evidence_insufficient"):
            await connection.execute("UPDATE qc_rule_execution SET execution_status=$1", status)
            assert (
                await repository.update_human_verdict(
                    execution_id=execution_id,
                    verdict="pass",
                    note=None,
                    operator=None,
                )
                is None
            )
        await connection.execute(
            "UPDATE qc_rule_execution SET execution_status='manual_review', "
            "judge_output='{}'::jsonb"
        )
        await connection.execute("UPDATE qc_task SET deleted_at=now()")
        assert (
            await repository.update_human_verdict(
                execution_id=execution_id,
                verdict="pass",
                note=None,
                operator=None,
            )
            is None
        )
        assert await connection.fetchval("SELECT human_verdict FROM qc_rule_execution") is None
    finally:
        await connection.close()
