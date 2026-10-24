"""用隔离 PostgreSQL 参数查询核对真实业务完成资格，不创建或修改业务记录。"""

import json
import os

import asyncpg
import pytest

from services.api.app.application.task_progress import rule_execution_counts_as_completed
from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql


@pytest.mark.parametrize(
    ("status", "reason", "bootstrap", "summary", "conflicts", "completed"),
    [
        ("pass", None, False, None, [], True),
        ("fail", None, False, None, [], True),
        ("warning", None, False, None, [], True),
        ("manual_review", "business_uncertain", False, None, [], True),
        ("skipped", "product_not_applicable", False, None, [], True),
        ("skipped", None, True, None, [], False),
        ("skipped", "exploration_not_started", False, None, [], False),
        ("skipped", "candidate_not_executed", False, None, [], False),
        ("skipped", "auth_requirement_not_met", False, None, [], False),
        ("skipped", "not_matched_by_run", None, None, [], False),
        ("skipped", " AUTH_REQUIREMENT_NOT_MET ", False, None, [], False),
        ("manual_review", "semantic_provider_http_500", False, None, [], False),
        ("manual_review", None, False, "模型 API 返回 HTTP 500，未生成结论", [], False),
        ("manual_review", None, False, None, ["semantic_provider_http_500"], False),
        ("manual_review", None, False, None, ["prompt_input_token_budget"], False),
        ("evidence_insufficient", None, False, None, [], False),
        ("error", None, False, None, [], False),
        ("pending", None, False, None, [], False),
    ],
)
async def test_completion_sql_matches_business_results_and_python_read_side(
    status, reason, bootstrap, summary, conflicts, completed
) -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("仅在显式设置隔离 ALIOTH_TEST_DATABASE_URL 时执行真实 SQL")
    connection = await asyncpg.connect(database_url)
    try:
        result = await connection.fetchval(
            f"""
            SELECT {completed_rule_execution_sql("execution")}
            FROM (
                SELECT $1::text AS execution_status,
                       $2::jsonb AS judge_input,
                       $3::jsonb AS judge_output,
                       $4::text AS result_summary,
                       NULL::text AS error_code
            ) AS execution
            """,
            status,
            json.dumps({"bootstrap": bootstrap}),
            json.dumps({"reason": reason, "model_output": {"conflicts": conflicts}}),
            summary,
        )
        assert result is completed
        # 详情仓储会把模型冲突中的平台诊断投影为 execution_reason。
        read_reason = conflicts[0] if conflicts else reason
        assert rule_execution_counts_as_completed(
            status, read_reason, is_bootstrap=bool(bootstrap), result_summary=summary
        ) is completed
    finally:
        await connection.close()


@pytest.mark.parametrize("field", ["error_code", "output_error_code", "reason"])
@pytest.mark.parametrize(
    "code", ["SUPPLEMENTAL_EVIDENCE_DISABLED", "SUPPLEMENTAL_EVIDENCE_NOT_READY"]
)
async def test_completion_sql_excludes_legacy_supplemental_denials(field: str, code: str) -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("仅在显式设置隔离 ALIOTH_TEST_DATABASE_URL 时执行真实 SQL")
    connection = await asyncpg.connect(database_url)
    try:
        output = {"reason": code if field == "reason" else None}
        if field == "output_error_code":
            output["error_code"] = code
        result = await connection.fetchval(
            f"""
            SELECT {completed_rule_execution_sql("execution")}
            FROM (
                SELECT 'skipped'::text AS execution_status,
                       '{{}}'::jsonb AS judge_input,
                       $1::jsonb AS judge_output,
                       NULL::text AS result_summary,
                       $2::text AS error_code
            ) AS execution
            """,
            json.dumps(output), code if field == "error_code" else None,
        )
        assert result is False
    finally:
        await connection.close()
