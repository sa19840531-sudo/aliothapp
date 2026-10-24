"""隔离 PostgreSQL：多流程冻结、归属反例、并发与历史兼容。"""

import asyncio
import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.journey_definitions import (
    JourneyDefinitionService,
    JourneyVersionConflictError,
)
from services.api.app.domain.journey import parse_journey_definition, parse_journey_execution_plan
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离数据库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition
from services.api.tests.test_journey_definitions import _definition


async def test_scoped_plans_coexist_and_preserve_legacy_payload_and_creation_time():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        repository = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=repository)
        legacy_payload = _definition().to_dict()
        legacy_payload["definition_id"] = f"login-{uuid4()}"
        legacy_definition = parse_journey_definition(legacy_payload)
        await service.publish(legacy_definition)
        legacy = await service.create_execution_plan(
            task_id=ids["task"],
            definition_id=legacy_definition.definition_id,
            version=legacy_definition.version,
        )
        before = await connection.fetchrow(
            "SELECT plan_payload, created_at FROM qc_journey_execution_plan "
            "WHERE execution_plan_id=$1",
            legacy.execution_plan_id,
        )
        plans = []
        for prefix in ("player", "payment"):
            definition = parse_journey_definition(checkpoint_definition(prefix))
            payload = definition.to_dict()
            payload["definition_id"] = f"{prefix}-{uuid4()}"
            await service.publish(parse_journey_definition(payload))
            plans.append(
                await service.create_run_execution_plan(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    definition_id=payload["definition_id"],
                    version=1,
                    scenario_key="manual",
                    parameters={},
                )
            )
        assert plans[0] != plans[1]
        assert await repository.get_execution_plan_for_task(task_id=ids["task"]) == legacy
        after = await connection.fetchrow(
            "SELECT plan_payload, created_at FROM qc_journey_execution_plan "
            "WHERE execution_plan_id=$1",
            legacy.execution_plan_id,
        )
        assert before == after
        listed = await repository.list_execution_plans_for_run(
            task_id=ids["task"],
            agent_run_id=ids["run"],
        )
        assert {plan.execution_plan_id for plan in listed} == {
            plan.execution_plan_id for plan in plans
        }


async def test_concurrent_freeze_is_idempotent_and_parameter_drift_does_not_overwrite():
    async with _database(surface_kind="web", target_platform="h5") as (_, pool, ids):
        repository = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=repository)
        payload = checkpoint_definition(f"player-{uuid4()}")
        await service.publish(parse_journey_definition(payload))
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            definition_id=payload["definition_id"],
            version=1,
            scenario_key="manual",
            parameters={"media_kind": "video"},
        )
        plans = await asyncio.gather(
            *(service.create_run_execution_plan(**kwargs) for _ in range(8))
        )
        assert all(plan == plans[0] for plan in plans)
        with pytest.raises(JourneyVersionConflictError):
            await service.create_run_execution_plan(
                **(kwargs | {"parameters": {"media_kind": "audio"}})
            )
        assert (
            await repository.get_execution_plan(execution_plan_id=plans[0].execution_plan_id)
            == plans[0]
        )


async def test_conflicting_concurrent_freeze_has_one_winner_without_replacing_it():
    async with _database(surface_kind="web", target_platform="h5") as (_, pool, ids):
        repository = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=repository)
        payload = checkpoint_definition(f"player-{uuid4()}")
        await service.publish(parse_journey_definition(payload))
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            definition_id=payload["definition_id"],
            version=1,
            scenario_key="manual",
        )
        results = await asyncio.gather(
            service.create_run_execution_plan(**kwargs, parameters={"media_kind": "video"}),
            service.create_run_execution_plan(**kwargs, parameters={"media_kind": "audio"}),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, Exception)]
        winners = [item for item in results if not isinstance(item, Exception)]
        assert len(winners) == len(failures) == 1
        assert isinstance(failures[0], JourneyVersionConflictError)
        assert await repository.list_execution_plans_for_run(
            task_id=ids["task"],
            agent_run_id=ids["run"],
        ) == (winners[0],)


async def test_foreign_run_rejected_without_inserting_any_plan():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        foreign_task, foreign_run = str(uuid4()), str(uuid4())
        await connection.execute(
            "INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,target_platform) "
            "SELECT $2::uuid,target_url,ruleset_id,submitter_id,tenant_id,target_platform "
            "FROM qc_task WHERE id=$1::uuid",
            ids["task"],
            foreign_task,
        )
        await connection.execute(
            "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name) "
            "VALUES($1::uuid,$2::uuid,'explorer','foreign-run')",
            foreign_run,
            foreign_task,
        )
        try:
            repository = PostgresJourneyDefinitionRepository(pool=pool)
            service = JourneyDefinitionService(repository=repository)
            payload = checkpoint_definition(f"player-{uuid4()}")
            await service.publish(parse_journey_definition(payload))
            with pytest.raises(JourneyVersionConflictError, match="归属"):
                await service.create_run_execution_plan(
                    task_id=ids["task"],
                    agent_run_id=foreign_run,
                    definition_id=payload["definition_id"],
                    version=1,
                    scenario_key="manual",
                    parameters={},
                )
            assert (
                await repository.list_execution_plans_for_run(
                    task_id=ids["task"],
                    agent_run_id=foreign_run,
                )
                == ()
            )
        finally:
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", foreign_task)


async def test_database_rejects_payload_scope_drift_and_rollback_that_discards_run_scope():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        repository = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=repository)
        payload = checkpoint_definition(f"player-{uuid4()}")
        await service.publish(parse_journey_definition(payload))
        plan = await service.create_run_execution_plan(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            definition_id=payload["definition_id"],
            version=1,
            scenario_key="manual",
            parameters={},
        )
        for bad in ("{}", '{"agent_run_id":"other-run"}'):
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE qc_journey_execution_plan SET plan_payload=$2::jsonb "
                    "WHERE execution_plan_id=$1",
                    plan.execution_plan_id,
                    bad,
                )
        assert await repository.get_execution_plan(execution_plan_id=plan.execution_plan_id) == plan
        forged = deepcopy(plan.to_dict())
        forged["scenario_key"] = "forged"
        forged["checkpoints"][0]["expected"]["media_ready"] = False
        del forged["execution_plan_id"]
        forged["execution_plan_id"] = f"journey-plan:{stable_hash(forged)}"
        forged_plan = parse_journey_execution_plan(forged)
        assert await repository.add_execution_plan(plan=forged_plan) is False
        assert (
            await repository.get_execution_plan(
                execution_plan_id=forged_plan.execution_plan_id,
            )
            is None
        )
        migration = importlib.import_module(
            "services.api.migrations.versions.20261016_0135_journey_run_plans"
        )
        statements = []
        with patch.object(migration, "op", SimpleNamespace(execute=statements.append)):
            migration.downgrade()
        with pytest.raises(asyncpg.RaiseError, match="禁止回滚"):
            async with connection.transaction():
                for statement in statements:
                    await connection.execute(statement)
        assert await repository.get_execution_plan(execution_plan_id=plan.execution_plan_id) == plan


async def test_terminal_run_cannot_freeze_new_work_but_history_remains_readable():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        repository = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=repository)
        payload = checkpoint_definition(f"player-{uuid4()}")
        await service.publish(parse_journey_definition(payload))
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            definition_id=payload["definition_id"],
            version=1,
            scenario_key="manual",
            parameters={},
        )
        plan = await service.create_run_execution_plan(**kwargs)
        await connection.execute(
            "UPDATE qc_agent_run SET status='done' WHERE id=$1::uuid", ids["run"]
        )
        assert await service.create_run_execution_plan(**kwargs) == plan
        with pytest.raises(JourneyVersionConflictError, match="归属"):
            await service.create_run_execution_plan(**(kwargs | {"scenario_key": "auto"}))


async def test_upgrade_preserves_preexisting_v1_plan_and_evidence_without_backfill():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        repository = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=repository)
        payload = _definition().to_dict()
        payload["definition_id"] = f"legacy-{uuid4()}"
        await service.publish(parse_journey_definition(payload))
        plan = await service.create_execution_plan(
            task_id=ids["task"],
            definition_id=payload["definition_id"],
            version=1,
        )
        before = await connection.fetchrow(
            "SELECT plan_payload,created_at FROM qc_journey_execution_plan "
            "WHERE execution_plan_id=$1",
            plan.execution_plan_id,
        )
        evidence_before = await connection.fetch(
            "SELECT manifest_id,manifest_payload FROM qc_evidence_manifest "
            "WHERE task_id=$1::uuid ORDER BY manifest_id",
            ids["task"],
        )
        migration = importlib.import_module(
            "services.api.migrations.versions.20261016_0135_journey_run_plans"
        )
        async with connection.transaction():
            # 共享隔离库可能残留其它用例的 Run 计划；downgrade 按全表扫描闸门，
            # 先只保留本用例的 V1 计划再模拟升级。子表有 RESTRICT FK，须先清引用。
            keep_plan_id = plan.execution_plan_id
            await connection.execute(
                "DELETE FROM qc_journey_checkpoint_attempt WHERE execution_plan_id <> $1",
                keep_plan_id,
            )
            await connection.execute(
                "DELETE FROM qc_journey_input_contract WHERE execution_plan_id <> $1",
                keep_plan_id,
            )
            await connection.execute(
                "DELETE FROM qc_journey_execution WHERE execution_plan_id <> $1",
                keep_plan_id,
            )
            await connection.execute(
                "UPDATE qc_restoration_attempt SET replay_execution_plan_id = NULL "
                "WHERE replay_execution_plan_id IS NOT NULL AND replay_execution_plan_id <> $1",
                keep_plan_id,
            )
            await connection.execute(
                "DELETE FROM qc_journey_execution_plan WHERE execution_plan_id <> $1",
                keep_plan_id,
            )
            # 仅在独立测试库模拟带旧计划的升级，正常服务不运行这些语句。
            for migrate in (migration.downgrade, migration.upgrade):
                statements = []
                with patch.object(migration, "op", SimpleNamespace(execute=statements.append)):
                    migrate()
                for statement in statements:
                    await connection.execute(statement)
            after = await connection.fetchrow(
                "SELECT plan_payload,created_at FROM qc_journey_execution_plan "
                "WHERE execution_plan_id=$1",
                plan.execution_plan_id,
            )
            assert after == before
            evidence_after = await connection.fetch(
                "SELECT manifest_id,manifest_payload FROM qc_evidence_manifest "
                "WHERE task_id=$1::uuid ORDER BY manifest_id",
                ids["task"],
            )
            assert evidence_before == evidence_after
        assert await repository.get_execution_plan_for_task(task_id=ids["task"]) == plan
