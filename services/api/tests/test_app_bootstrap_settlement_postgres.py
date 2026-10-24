"""真实 PostgreSQL 验证 APP 启动失败不生成没有页面依据的规则执行记录。"""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.rule_execution import (
    CandidatePageReference,
    RuntimePageInspectionFailureFallback,
    RuntimeRuleExecutionFallback,
)
from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    build_task_rule_inventory,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _database,
    _insert_capture,
    _SingleConnectionPool,
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库完整迁移
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


async def _configure(connection, ids, *, platform="app", frozen=True):
    rules = []
    for index, auth in enumerate((None, "authenticated")):
        rule = {
            "rule_id": str(uuid4()), "rule_code": f"BOOTSTRAP_{uuid4().hex}",
            "name": f"启动收尾规则{index}", "category": "text", "severity": "medium",
            "executor_type": "llm", "check_point": "核对页面文字",
            "applicable_page_types": ["home"],
            "input_contract": {"target_clients": ["APP"]}, "judge_config": {},
            "manual_review_policy": {}, "steps": [], "channel": None, "sort_order": index,
        }
        if auth:
            rule["input_contract"]["auth_requirement"] = auth
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,severity,
                    executor_type,check_point,applicable_page_types,input_contract)
               VALUES($1::uuid,$2::uuid,$3,$4,'text','medium','llm','核对页面文字',
                      '["home"]'::jsonb,$5::jsonb)""",
            rule["rule_id"], ids["ruleset"], rule["rule_code"], rule["name"],
            json.dumps(rule["input_contract"]),
        )
        rules.append(rule)
    config = {
        "workflow_version": "v2", "execution_mode": "full_inspection",
        "inspection_scope": {"page_types": ["home"]}, "ruleset_rule_total": len(rules),
    }
    if frozen:
        config["rule_inventory"] = build_task_rule_inventory(
            ruleset_id=ids["ruleset"], ruleset_version=1,
            ruleset_snapshot_hash="app-evidence-index-test", target_platform=platform,
            channel="workbench", task_config=config, rules=rules,
        )
    await connection.execute(
        """UPDATE qc_task SET target_platform=$2::varchar,
                  target_url=CASE WHEN $2::varchar='app' THEN NULL ELSE 'https://example.com/' END,
                  task_config=task_config || $3::jsonb,
                  finalization_agent_run_id=$4::uuid WHERE id=$1::uuid""",
        ids["task"], platform, json.dumps(config), ids["run"],
    )
    return rules, config


async def _settle(connection, ids, *, candidates=(), rule_failures=(), page_failures=()):
    repository = PostgresTaskRepository(pool=_SingleConnectionPool(connection))
    return await repository.finalize_missing_rule_executions(
        task_id=ids["task"], agent_run_id=ids["run"],
        finished_at=datetime.now(UTC), candidate_pages=list(candidates),
        runtime_rule_fallbacks=list(rule_failures),
        runtime_page_failure_fallbacks=list(page_failures),
        auth_gate="authenticated",
    )


@pytest.mark.parametrize("candidate_capture", [None, "missing"])
async def test_frozen_app_without_real_capture_creates_no_execution(candidate_capture):
    async with _database() as (connection, ids):
        rules, _ = await _configure(connection, ids)
        candidates = [] if candidate_capture is None else [
            CandidatePageReference(page_capture_id=str(uuid4()), page_type="home"),
        ]
        for _ in range(2):
            result = await _settle(connection, ids, candidates=candidates)
            assert result.inserted_count == result.unresolved_candidate_count == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == 0
        assert len(rules) == 2


@pytest.mark.parametrize("invalid", ["hash", "shape"])
async def test_damaged_inventory_cannot_fall_back_to_live_bootstrap(invalid):
    async with _database() as (connection, ids):
        _, config = await _configure(connection, ids)
        if invalid == "hash":
            config["rule_inventory"]["inventory_hash"] = "damaged"
        else:
            config["rule_inventory"] = []
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config || $2::jsonb WHERE id=$1::uuid",
            ids["task"], json.dumps(config),
        )
        with pytest.raises(TaskRuleInventoryError):
            await _settle(connection, ids)
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == 0


async def test_declared_null_inventory_does_not_create_legacy_execution():
    async with _database() as (connection, ids):
        await _configure(connection, ids)
        await connection.execute(
            """UPDATE qc_task SET task_config=jsonb_set(task_config,'{rule_inventory}','null')
               WHERE id=$1::uuid""", ids["task"],
        )
        assert (await _settle(connection, ids)).inserted_count == 0


async def test_real_runtime_failure_uses_frozen_contract_after_live_changes():
    async with _database() as (connection, ids):
        rules, _ = await _configure(connection, ids)
        rule = rules[1]
        capture = await _insert_capture(
            connection, ids, capture_role="observation", semantic_role="final_observation",
        )
        await connection.execute(
            """UPDATE qc_rule SET status='disabled', rule_code=$2, severity='critical',
                      executor_type='hybrid', applicable_page_types='["profile"]'::jsonb,
                      input_contract='{"auth_requirement":"guest"}'::jsonb
               WHERE id=$1::uuid""", rule["rule_id"], f"changed_{uuid4().hex}",
        )
        result = await _settle(
            connection, ids,
            candidates=[CandidatePageReference(
                page_capture_id=capture, page_type="home",
                candidate_rule_codes=(rule["rule_code"],),
            )],
            rule_failures=[RuntimeRuleExecutionFallback(
                page_capture_id=capture, rule_id=rule["rule_id"], rule_code=rule["rule_code"],
                execution_status="error", result_summary="规则写回失败",
                problem_desc="数据库暂不可写",
            )],
        )
        assert result.inserted_count == 1
        row = await connection.fetchrow(
            "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        )
        assert row["execution_status"] == "error"
        assert row["judge_strategy"] == "llm" and row["severity"] == "medium"
        assert json.loads(row["judge_input"])["rule_code"] == rule["rule_code"]
        assert json.loads(row["judge_input"])["bootstrap"] is False
        assert json.loads(row["judge_output"])["reason"] == "runtime_rule_write_failed"


async def test_unexecuted_real_candidate_does_not_guess_guest_not_applicable():
    async with _database() as (connection, ids):
        rules, _ = await _configure(connection, ids)
        rule = rules[1]
        capture = await _insert_capture(
            connection, ids, capture_role="observation", semantic_role="final_observation",
        )
        result = await _settle(connection, ids, candidates=[CandidatePageReference(
            page_capture_id=capture, page_type="home", candidate_rule_codes=(rule["rule_code"],),
        )])
        assert result.inserted_count == result.unresolved_candidate_count == 1
        row = await connection.fetchrow(
            "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        )
        assert row["execution_status"] == "skipped"
        assert json.loads(row["judge_input"])["bootstrap"] is True
        assert json.loads(row["judge_output"])["reason"] == "candidate_not_executed"
        assert "不适用" not in row["result_summary"]


async def test_real_page_failure_is_still_persisted_for_candidates():
    async with _database() as (connection, ids):
        await _configure(connection, ids)
        capture = await _insert_capture(
            connection, ids, capture_role="observation", semantic_role="final_observation",
        )
        result = await _settle(connection, ids, page_failures=[RuntimePageInspectionFailureFallback(
            page_capture_id=capture, page_type="home", failure_code="CAPTURE_FAILED",
            failure_message="真实页面采集失败",
        )])
        assert result.inserted_count == 2
        rows = await connection.fetch(
            "SELECT judge_output FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        )
        assert all(
            json.loads(row["judge_output"])["reason"] == "runtime_page_inspection_failed"
            for row in rows
        )


@pytest.mark.parametrize("platform", ["app", "h5", "web"])
async def test_legacy_and_web_backfill_behaviour_is_preserved(platform):
    async with _database() as (connection, ids):
        await _configure(connection, ids, platform=platform, frozen=False)
        result = await _settle(connection, ids)
        assert result.inserted_count == 2
        reasons = await connection.fetch(
            """SELECT judge_output->>'reason' AS reason FROM qc_rule_execution
               WHERE task_id=$1::uuid""", ids["task"],
        )
        assert {row["reason"] for row in reasons} == {
            "exploration_not_started", "auth_requirement_not_met",
        }


async def test_new_settlement_preserves_prior_run_original_record():
    async with _database() as (connection, ids):
        rules, _ = await _configure(connection, ids)
        old_run, execution = str(uuid4()), str(uuid4())
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','历史运行','done')""", old_run, ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(id,task_id,rule_id,agent_run_id,
                      execution_status,judge_strategy,result_summary,judge_input)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'skipped','llm','历史原文',
                      '{"bootstrap":true,"candidate_matched":true}'::jsonb)""",
            execution, ids["task"], rules[0]["rule_id"], old_run,
        )
        before = await connection.fetchrow(
            "SELECT * FROM qc_rule_execution WHERE id=$1::uuid", execution,
        )
        assert (await _settle(connection, ids)).inserted_count == 0
        assert await connection.fetchrow(
            "SELECT * FROM qc_rule_execution WHERE id=$1::uuid", execution,
        ) == before
