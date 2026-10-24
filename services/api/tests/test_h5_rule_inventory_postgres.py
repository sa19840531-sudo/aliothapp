"""在原 PostgreSQL 内回滚验证清单冻结、候选与门禁，不创建数据库或真实任务。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.h5_failed_run_finalization import terminate_failed_h5_task
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemRule,
    PublishInspectionWorkItemCommand,
)
from services.api.app.application.report_rule_inventory import project_report_rule_inventory
from services.api.app.application.task_finalization import TaskFinalizationService
from services.api.app.application.task_rule_inventory import (
    inventory_execution_contract_hash,
    parse_task_rule_inventory,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import TaskExecutionStatus, TaskFinalizationState
from services.api.app.domain.task.completion import CompletionGateFacts
from services.api.app.domain.task.targets import TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.db.h5_rule_inventory_completion_gate import (
    apply_h5_rule_inventory_gate,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    publish_inspection_work_item_on_connection,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    _GATE_FACTS_SQL,
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    PostgresTaskRuleInventoryRepository,
    freeze_task_rule_inventory_on_connection,
)
from services.api.app.infrastructure.db.task_rule_inventory_sql import (
    frozen_h5_rule_identity_sql,
    rule_execution_target_sql,
)
from services.api.tests.test_targeted_recapture_chain_postgres import _seed

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需显式提供事务回滚测试连接",
)


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def seeded():
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction(isolation="repeatable_read")
    await transaction.start()
    try:
        ids = {
            key: str(uuid4())
            for key in (
                "tenant",
                "user",
                "ruleset",
                "rule",
                "task",
                "run",
                "work_item",
                "attempt",
                "original",
                "restored",
            )
        }
        await _seed(connection, ids)
        await connection.execute(
            "UPDATE qc_rule SET applicable_page_types='[\"home\"]'::jsonb, "
            'input_contract=\'{"target_clients":["H5"]}\'::jsonb WHERE id=$1::uuid',
            ids["rule"],
        )
        config = await freeze_task_rule_inventory_on_connection(
            connection,
            target_platform="h5",
            ruleset_code=f"recapture-chain-{ids['ruleset']}",
            ruleset_version=1,
            channel="workbench",
            task_config={"workflow_version": "v2", "execution_mode": "full_inspection"},
        )
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5', channel='workbench', task_config=$2::jsonb, "
            "finalization_agent_run_id=$3::uuid WHERE id=$1::uuid",
            ids["task"],
            json.dumps(config),
            ids["run"],
        )
        yield connection, Pool(connection), ids, config
    finally:
        await transaction.rollback()
        await connection.close()


async def test_h5_submission_atomically_freezes_server_rules_and_rejects_client_inventory():
    async with seeded() as (_, pool, ids, _):
        created = await PostgresTaskRepository(pool=pool).create_task(
            access_scope=AccessScope(
                actor_user_id=ids["user"],
                tenant_id=ids["tenant"],
                permissions=frozenset({"taskObjects:write:any"}),
            ),
            target=TaskTarget(platform=TaskTargetPlatform.H5, target_url="https://sample.test/"),
            ruleset_code=f"recapture-chain-{ids['ruleset']}",
            ruleset_version=1,
            channel="workbench",
            priority=50,
            task_config={"workflow_version": "v2", "rule_inventory": {"rules": []}},
            page_sampling_policy={},
            loop_guard={},
        )
        frozen = parse_task_rule_inventory(created.task_config)
        assert frozen["rule_count"] == 1
        assert frozen["rules"][0]["rule_id"] == ids["rule"]
        assert created.page_sampling_policy["rule_target_page_types"] == ["home"]


async def test_h5_candidate_definition_and_write_identity_survive_live_rule_edit():
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_rule SET rule_code='edited-rule', name='edited', status='disabled' "
            "WHERE id=$1::uuid",
            ids["rule"],
        )
        repository = PostgresTaskRepository(pool=pool)
        candidates = await repository.list_candidate_rules(task_id=ids["task"], page_type="home")
        assert [(rule.rule_id, rule.rule_code) for rule in candidates] == [
            (ids["rule"], "alioth_chain_1"),
        ]
        definition = await repository.get_rule_judge_definition(
            task_id=ids["task"],
            rule_code="alioth_chain_1",
        )
        assert definition.rule_name == "Chain Rule"
        row = await connection.fetchrow(
            rule_execution_target_sql(frozen=True),
            ids["task"],
            ids["rule"],
            "alioth_chain_1",
        )
        assert row["rule_code"] == "alioth_chain_1"


async def test_inventory_snapshot_uses_current_owner_and_keeps_untriggered_rule_visible():
    async with seeded() as (_, pool, ids, _):
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"],
            agent_run_id=ids["run"],
        )
        assert snapshot["inventory_status"] == "frozen"
        assert snapshot["agent_run_id"] == ids["run"]
        insight = {"summary": {}, "rule_items": []}
        project_report_rule_inventory(insight, snapshot)
        assert len(insight["rule_items"]) == 1
        assert insight["rule_items"][0]["execution_id"] is None
        assert insight["summary"]["rule_inventory_pending_rules"] == 1


async def test_gate_does_not_pass_when_frozen_rule_has_no_execution():
    async with seeded() as (connection, _, ids, config):
        facts = CompletionGateFacts(
            exploration_terminal=True,
            pending_action_count=0,
            pending_capture_commit_count=0,
            nonterminal_enforced_work_item_count=0,
            active_session_request_count=0,
            candidate_rule_count=0,
            terminal_candidate_rule_count=0,
            background_execution_count=0,
            review_policy_satisfied=True,
        )
        result = await apply_h5_rule_inventory_gate(
            connection,
            task_id=ids["task"],
            facts=facts,
            row={
                "inventory_target_platform": "h5",
                "inventory_task_config": config,
                "inventory_ruleset_id": ids["ruleset"],
                "inventory_channel": "workbench",
            },
        )
        assert facts.gate_passed
        assert not result.gate_passed
        assert result.candidate_rule_count == 1


async def test_historical_inventory_is_not_rebuilt_after_rules_changed():
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config-'rule_inventory', "
            "created_at=created_at-interval '1 day' WHERE id=$1::uuid",
            ids["task"],
        )
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"],
        )
        assert snapshot["inventory_status"] == "unavailable"
        assert snapshot["unavailable_reason"] == "historical_rules_changed"


async def test_gate_facts_sql_reads_the_frozen_inventory_on_original_database():
    async with seeded() as (connection, _, ids, config):
        row = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "succeeded")
        assert row["inventory_target_platform"] == "h5"
        assert (
            json.loads(row["inventory_task_config"])["rule_inventory"] == config["rule_inventory"]
        )
        assert str(row["inventory_ruleset_id"]) == ids["ruleset"]


async def test_frozen_identity_uses_h5_contract_without_relaxing_other_platforms():
    async with seeded() as (connection, _, ids, _):
        await connection.execute(
            "UPDATE qc_rule SET rule_code='edited-rule' WHERE id=$1::uuid",
            ids["rule"],
        )
        query = """
            SELECT __IDENTITY__ FROM (
                SELECT id, ruleset_id, task_config, $4::varchar AS target_platform
                FROM qc_task WHERE id=$1::uuid
            ) AS task
            JOIN qc_rule AS rule ON rule.id=$2::uuid WHERE task.id=$1::uuid
        """.replace(
            "__IDENTITY__",
            frozen_h5_rule_identity_sql(
                task="task",
                rule="rule",
                code="$3::varchar",
                ruleset="task.ruleset_id",
            ),
        )
        assert await connection.fetchval(query, ids["task"], ids["rule"], "alioth_chain_1", "h5")
        assert not await connection.fetchval(query, ids["task"], ids["rule"], "edited-rule", "h5")
        for platform in ("app", "web"):
            assert not await connection.fetchval(
                query,
                ids["task"],
                ids["rule"],
                "alioth_chain_1",
                platform,
            )
            assert await connection.fetchval(
                query,
                ids["task"],
                ids["rule"],
                "edited-rule",
                platform,
            )


async def test_published_h5_work_member_retains_frozen_rule_identity_after_live_edit():
    async with seeded() as (connection, _, ids, config):
        await connection.execute(
            "UPDATE qc_rule SET rule_code='edited-rule' WHERE id=$1::uuid",
            ids["rule"],
        )
        expected_hash = inventory_execution_contract_hash(config["rule_inventory"]["rules"][0])
        published = await publish_inspection_work_item_on_connection(
            connection,
            command=PublishInspectionWorkItemCommand(
                task_id=ids["task"],
                page_capture_id=ids["restored"],
                producer_agent_run_id=ids["run"],
                ruleset_id=ids["ruleset"],
                ruleset_version=1,
                ruleset_snapshot_hash="snapshot-1",
                batch_key="frozen-inventory:visual:snapshot",
                strategy="hybrid",
                session_dependency="snapshot",
                defer_wakeup=True,
                rules=(
                    InspectionWorkItemRule(
                        rule_id=ids["rule"],
                        rule_code="alioth_chain_1",
                        ordinal=0,
                        contract_hash=expected_hash,
                    ),
                ),
            ),
            input_hash="frozen-inventory-input",
        )
        member = await connection.fetchrow(
            "SELECT rule_code, contract_hash FROM qc_inspection_work_item_rule "
            "WHERE work_item_id=$1::uuid",
            published.work_item_id,
        )
        assert member["rule_code"] == "alioth_chain_1"
        assert member["contract_hash"] == expected_hash


@pytest.mark.parametrize("valid_proof", [True, False])
async def test_gate_reconciles_real_execution_only_with_matching_published_contract(valid_proof):
    async with seeded() as (connection, pool, ids, config):
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || '{\"finalization_epoch\":0}'::jsonb "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        execution_id = await connection.fetchval(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, agent_run_id, execution_status, judge_strategy
            ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'pass', 'hybrid')
            RETURNING id::text
            """,
            ids["task"],
            ids["rule"],
            ids["original"],
            ids["run"],
        )
        contract_hash = inventory_execution_contract_hash(config["rule_inventory"]["rules"][0])
        await connection.execute(
            "UPDATE qc_inspection_work_item_rule SET status='written', "
            "rule_execution_id=$2::uuid, contract_hash=$3 WHERE work_item_id=$1::uuid",
            ids["work_item"],
            execution_id,
            contract_hash if valid_proof else "other-contract",
        )
        facts = CompletionGateFacts(
            exploration_terminal=True,
            pending_action_count=0,
            pending_capture_commit_count=0,
            nonterminal_enforced_work_item_count=0,
            active_session_request_count=0,
            candidate_rule_count=1,
            terminal_candidate_rule_count=1,
            background_execution_count=0,
            review_policy_satisfied=True,
        )
        result = await apply_h5_rule_inventory_gate(
            connection,
            task_id=ids["task"],
            facts=facts,
            row={
                "inventory_target_platform": "h5",
                "inventory_task_config": config,
                "inventory_ruleset_id": ids["ruleset"],
                "inventory_channel": "workbench",
            },
        )
        assert result.gate_passed is valid_proof
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"],
        )
        assert snapshot["executions"][0]["proof_contract_hash"] == (
            contract_hash if valid_proof else "other-contract"
        )


@pytest.mark.parametrize("attempt_inflight", [True, False])
async def test_missing_rule_inventory_results_do_not_prevent_failed_h5_termination(
    attempt_inflight,
):
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_agent_run SET status='failed' WHERE id=$1::uuid",
            ids["run"],
        )
        service = TaskFinalizationService(PostgresTaskFinalizationRepository(pool=pool))
        if attempt_inflight:
            with pytest.raises(
                TaskFinalizationPersistenceError, match="H5_EXECUTION_RESOURCES_NOT_DRAINED"
            ):
                await terminate_failed_h5_task(
                    service,
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    reason_code="h5_navigation_failed",
                )
            assert (
                await service.get(task_id=ids["task"])
            ).state is not TaskFinalizationState.SEALED
            return
        # 本例仅验证清单缺项不阻断失败终态；在途尝试的拒绝由上面的反例独立证明。
        await connection.execute(
            "UPDATE qc_inspection_attempt SET status='abandoned',finished_at=now() "
            "WHERE id=$1::uuid",
            ids["attempt"],
        )
        await terminate_failed_h5_task(
            service,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            reason_code="h5_navigation_failed",
        )
        result = await service.get(task_id=ids["task"])
        assert result.state is TaskFinalizationState.SEALED
        assert result.execution_status is TaskExecutionStatus.FAILED
        assert not await connection.fetchval(
            "SELECT gate_passed FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            ids["task"],
        )
