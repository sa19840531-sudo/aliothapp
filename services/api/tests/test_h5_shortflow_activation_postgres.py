"""隔离库验证启用事务、重复投递、配置漂移与预算不变。"""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.api.app.application.h5_shortflows import H5ShortflowError
from services.api.app.application.journey_definitions import (
    JourneyDefinitionService,
    JourneyVersionConflictError,
)
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.infrastructure.db.h5_shortflow_activation import (
    activate_h5_shortflows_for_run,
    freeze_h5_shortflows_on_connection,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_shortflow_activation import definition_payload, task_config


async def configure(connection, pool, ids):
    config = task_config()
    payload = definition_payload()
    payload["definition_id"] += "-" + ids["task"]
    definition = parse_journey_definition(payload)
    config["h5_shortflows"][0].update(
        definition_id=definition.definition_id, content_hash=definition.content_hash
    )
    service = JourneyDefinitionService(repository=PostgresJourneyDefinitionRepository(pool=pool))
    await service.publish(definition)
    frozen = await freeze_h5_shortflows_on_connection(connection, task_config=config)
    await connection.execute(
        "UPDATE qc_task SET status='running',task_config=$2::jsonb WHERE id=$1::uuid",
        ids["task"],
        json.dumps(frozen),
    )
    await connection.execute(
        "UPDATE qc_agent_run SET agent_type='main',status='running' WHERE id=$1::uuid", ids["run"]
    )
    return frozen


async def test_concurrent_activation_creates_one_plan_without_refreshing_run_budget():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        frozen = await configure(connection, pool, ids)
        from services.api.app.infrastructure.db.h5_llm_budget_repository import (
            PostgresH5LLMBudgetRepository,
        )

        budget = PostgresH5LLMBudgetRepository(pool=pool)
        await budget.initialize(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            max_calls=200,
            deadline=datetime.now(UTC) + timedelta(minutes=180),
        )
        await budget.reserve(task_id=ids["task"], consumer_agent_run_id=ids["run"])
        budget_before = await connection.fetchrow(
            "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid",
            ids["task"],
        )
        before = await connection.fetchrow(
            "SELECT started_at,loop_guard FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
        )
        kwargs = dict(task_id=ids["task"], agent_run_id=ids["run"], task_config=frozen, pool=pool)
        results = await asyncio.gather(
            *(activate_h5_shortflows_for_run(**kwargs) for _ in range(4))
        )
        assert all(result == results[0] for result in results)
        assert len(results[0]) == 1
        plan = results[0][0]
        assert plan.rule_contract_hashes == tuple(
            frozen["h5_shortflows"][0]["rule_contract_hashes"]
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_execution_plan WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
        assert before == await connection.fetchrow(
            "SELECT started_at,loop_guard FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
        )
        assert budget_before == await connection.fetchrow(
            "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid",
            ids["task"],
        )


@pytest.mark.parametrize("fault", ["queue", "skill", "canceled", "foreign_run", "definition"])
async def test_drift_or_terminal_identity_never_creates_a_plan(fault):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        frozen = await configure(connection, pool, ids)
        queued = deepcopy(frozen)
        run = ids["run"]
        if fault == "queue":
            queued["h5_shortflows"][0]["scenario_key"] = "foreign"
        elif fault == "skill":
            frozen["h5_shortflows"][0]["workflow_skill_hash"] = "0" * 64
            await connection.execute(
                "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
                ids["task"],
                json.dumps(frozen),
            )
            queued = frozen
        elif fault == "canceled":
            await connection.execute(
                "UPDATE qc_task SET status='canceled' WHERE id=$1::uuid", ids["task"]
            )
        elif fault == "foreign_run":
            run = ids["task"]
        else:
            item = frozen["h5_shortflows"][0]
            await connection.execute(
                "DELETE FROM qc_journey_definition WHERE definition_id=$1", item["definition_id"]
            )
        with pytest.raises(H5ShortflowError):
            await activate_h5_shortflows_for_run(
                task_id=ids["task"], agent_run_id=run, task_config=queued, pool=pool
            )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_execution_plan WHERE task_id=$1::uuid", ids["task"]
            )
            == 0
        )


async def test_second_plan_conflict_rolls_back_first_in_same_activation_transaction():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        frozen = await configure(connection, pool, ids)
        # 两个不同冻结规则，只让第二个场景预先占用；不能留下半启用的第一个场景。
        from services.api.app.application.task_rule_inventory import build_task_rule_inventory

        config = task_config()
        first = config["h5_shortflows"][0]
        first.update(
            definition_id=frozen["h5_shortflows"][0]["definition_id"],
            content_hash=frozen["h5_shortflows"][0]["content_hash"],
        )
        second = deepcopy(first)
        second.update(scenario_key="other", rule_ids=["rule-other"])
        config["h5_shortflows"].append(second)
        rules = deepcopy(config["rule_inventory"]["rules"])
        rule = deepcopy(rules[0])
        rule.update(rule_id="rule-other", rule_code="player-other", check_point="other")
        rules.append(rule)
        config["ruleset_rule_total"] = 2
        config["rule_inventory"] = build_task_rule_inventory(
            ruleset_id="ruleset",
            ruleset_version=1,
            ruleset_snapshot_hash="rules",
            target_platform="h5",
            channel="api",
            task_config=config,
            rules=rules,
        )
        frozen = await freeze_h5_shortflows_on_connection(connection, task_config=config)
        await connection.execute(
            "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
            ids["task"],
            json.dumps(frozen),
        )
        service = JourneyDefinitionService(
            repository=PostgresJourneyDefinitionRepository(pool=pool)
        )
        occupied = await service.create_run_execution_plan(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            definition_id=second["definition_id"],
            version=1,
            scenario_key="other",
            parameters={"different": True},
        )
        with pytest.raises(JourneyVersionConflictError):
            await activate_h5_shortflows_for_run(
                task_id=ids["task"], agent_run_id=ids["run"], task_config=frozen, pool=pool
            )
        listed = await PostgresJourneyDefinitionRepository(pool=pool).list_execution_plans_for_run(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert listed == (occupied,)


@pytest.mark.parametrize(
    "fault",
    ["newer_main", "wrong_owner", "exploration_draining", "session_draining", "queue_version"],
)
async def test_shortflow_cannot_activate_from_stale_main_or_draining_task(fault):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        frozen = await configure(connection, pool, ids)
        before = await connection.fetchrow(
            "SELECT started_at,loop_guard FROM qc_agent_run WHERE id=$1::uuid",
            ids["run"],
        )
        if fault in {"newer_main", "wrong_owner"}:
            other = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at) "
                "VALUES($1::uuid,$2::uuid,'main','shortflow-owner-test','running',"
                "now()+CASE WHEN $3 THEN interval '1 second' ELSE interval '-1 minute' END)",
                other,
                ids["task"],
                fault == "newer_main",
            )
            if fault == "wrong_owner":
                await connection.execute(
                    "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
                    ids["task"],
                    other,
                )
        elif fault == "queue_version":
            frozen = {**frozen, "workflow_version": "v1"}
        else:
            await connection.execute(
                "UPDATE qc_task SET finalization_state=$2::varchar,exploration_draining_at=now(),"
                "session_draining_at=CASE WHEN $2::varchar='session_draining' "
                "THEN now() ELSE NULL END "
                "WHERE id=$1::uuid",
                ids["task"],
                fault,
            )
        with pytest.raises(H5ShortflowError):
            await activate_h5_shortflows_for_run(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                task_config=frozen,
                pool=pool,
            )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_execution_plan WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 0
        )
        assert before == await connection.fetchrow(
            "SELECT started_at,loop_guard FROM qc_agent_run WHERE id=$1::uuid",
            ids["run"],
        )


async def test_current_main_with_old_history_can_activate_idempotently():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        frozen = await configure(connection, pool, ids)
        await connection.execute(
            "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at) "
            "VALUES($1::uuid,$2::uuid,'main','historical-shortflow-main','done',"
            "now()-interval '1 day')",
            str(uuid4()),
            ids["task"],
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        kwargs = dict(task_id=ids["task"], agent_run_id=ids["run"], task_config=frozen, pool=pool)
        first = await activate_h5_shortflows_for_run(**kwargs)
        assert len(first) == 1
        assert first == await activate_h5_shortflows_for_run(**kwargs)


@pytest.mark.parametrize("commit_replacement", [True, False])
async def test_activation_rechecks_latest_main_after_waiting_for_task_lock(commit_replacement):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        frozen = await configure(connection, pool, ids)
        async with pool.acquire() as writer:
            transaction = writer.transaction()
            await transaction.start()
            settled = False
            attempt = None
            try:
                # 新Run的外键持有任务行锁；让启用查询明确等在该事务之后。
                await writer.execute(
                    "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at) "
                    "VALUES($1::uuid,$2::uuid,'main','replacement-in-flight','running',"
                    "now()+interval '1 second')",
                    str(uuid4()),
                    ids["task"],
                )
                attempt = asyncio.create_task(
                    activate_h5_shortflows_for_run(
                        task_id=ids["task"],
                        agent_run_id=ids["run"],
                        task_config=frozen,
                        pool=pool,
                    )
                )
                async with asyncio.timeout(5):
                    while not await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE $1=ANY(pg_blocking_pids(pid)))",
                        writer.get_server_pid(),
                    ):
                        assert not attempt.done(), "启用查询没有等待受控任务行锁"
                        await asyncio.sleep(0.01)
                if commit_replacement:
                    await transaction.commit()
                else:
                    await transaction.rollback()
                settled = True
                if commit_replacement:
                    with pytest.raises(H5ShortflowError):
                        await attempt
                else:
                    assert len(await attempt) == 1
                assert await connection.fetchval(
                    "SELECT count(*) FROM qc_journey_execution_plan WHERE task_id=$1::uuid",
                    ids["task"],
                ) == (0 if commit_replacement else 1)
            finally:
                if not settled:
                    await transaction.rollback()
                if attempt is not None and not attempt.done():
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
