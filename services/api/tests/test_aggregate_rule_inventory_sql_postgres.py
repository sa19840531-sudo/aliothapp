"""真实 PostgreSQL 验证聚合义务沿用冻结清单，不受在线规则漂移影响。"""

import json
from uuid import uuid4

from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.infrastructure.db.aggregate_rule_inventory_sql import (
    aggregate_expected_rule_count_sql,
)
from services.api.app.infrastructure.db.task_finalization_repository import _GATE_FACTS_SQL
from services.api.tests.test_task_rule_inventory_postgres import _database


async def _expected(connection, task_id):
    return await connection.fetchval(
        f"SELECT {aggregate_expected_rule_count_sql('task')} "
        "FROM qc_task AS task WHERE task.id=$1::uuid",
        task_id,
    )


async def _gate_pending(connection, task_id):
    row = await connection.fetchrow(_GATE_FACTS_SQL, task_id, "done")
    return row["nonterminal_enforced_work_item_count"]


async def test_frozen_aggregate_expected_survives_live_add_delete_enable_and_config_edits():
    async with _database(include_aggregate=True) as (connection, _, ids, rules, *_):
        assert await _expected(connection, ids["task"]) == 1
        baseline = await _gate_pending(connection, ids["task"])
        await connection.execute(
            "UPDATE qc_rule SET status='disabled',judge_config='{}'::jsonb WHERE id=$1::uuid",
            rules["aggregate"],
        )
        assert await _expected(connection, ids["task"]) == 1
        assert await _gate_pending(connection, ids["task"]) == baseline
        await connection.execute(
            """UPDATE qc_rule SET judge_config=$2::jsonb WHERE id=$1::uuid""",
            rules["eligible"],
            json.dumps({"capability_config": {"evidence_input_scope": "task"}}),
        )
        added_rule = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,
                      status,check_point,judge_config)
               VALUES($1::uuid,$2::uuid,$3,'新聚合规则','visual','hybrid','high','enabled',
                      '检查内容','{"capability_config":{"evidence_input_scope":"task"}}'::jsonb)""",
            added_rule,
            ids["ruleset"],
            f"added_{added_rule}",
        )
        for status in ("disabled", "enabled"):
            await connection.execute(
                "UPDATE qc_rule SET status=$2 WHERE id=$1::uuid",
                added_rule,
                status,
            )
            assert await _expected(connection, ids["task"]) == 1
            assert await _gate_pending(connection, ids["task"]) == baseline
        await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rules["aggregate"])
        assert await _expected(connection, ids["task"]) == 1
        assert await _gate_pending(connection, ids["task"]) == baseline


async def test_historical_or_non_app_task_keeps_original_live_aggregate_expected():
    async with _database(include_aggregate=True) as (connection, _, ids, rules, *_):
        await connection.execute(
            "UPDATE qc_rule SET status='disabled' WHERE id=$1::uuid",
            rules["aggregate"],
        )
        await connection.execute(
            """UPDATE qc_task SET target_platform='web',target_url='https://inventory.test'
               WHERE id=$1::uuid""",
            ids["task"],
        )
        assert await _expected(connection, ids["task"]) == 0
        await connection.execute(
            """UPDATE qc_task SET target_platform='app',target_url=NULL,
                      task_config=task_config-'rule_inventory'
               WHERE id=$1::uuid""",
            ids["task"],
        )
        assert await _expected(connection, ids["task"]) == 0
        await connection.execute(
            "UPDATE qc_rule SET status='enabled' WHERE id=$1::uuid",
            rules["aggregate"],
        )
        assert await _expected(connection, ids["task"]) == 1


async def test_frozen_scope_exclusions_do_not_create_aggregate_obligations():
    async with _database() as (connection, _, ids, rules, _, persisted, _):
        config = dict(persisted.task.task_config)
        frozen = config["rule_inventory"]
        declarations = frozen["rules"]
        for rule in declarations:
            if rule["rule_id"] in {rules["scope"], rules["platform"]}:
                rule["judge_config"] = {
                    "capability_config": {"evidence_input_scope": "task"},
                }
        config["rule_inventory"] = build_task_rule_inventory(
            ruleset_id=frozen["ruleset_id"],
            ruleset_version=frozen["ruleset_version"],
            ruleset_snapshot_hash=frozen["ruleset_snapshot_hash"],
            target_platform="app",
            channel="workbench",
            task_config=config,
            rules=declarations,
        )
        await connection.execute(
            "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
            ids["task"],
            json.dumps(config),
        )
        assert await _expected(connection, ids["task"]) == 0
