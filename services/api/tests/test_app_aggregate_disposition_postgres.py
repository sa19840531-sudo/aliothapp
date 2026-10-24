"""真实 PostgreSQL 验证聚合不适用处置在清单门禁及报告的同源读取。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.task_rule_inventory import (
    build_task_rule_inventory,
    parse_task_rule_inventory,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task.completion import (
    CompletionGateFacts,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.app_rule_inventory_completion_gate import (
    apply_app_rule_inventory_gate,
)
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    PostgresTaskRuleInventoryRepository,
)
from services.api.tests.test_app_aggregate_recovery_postgres import _source
from services.api.tests.test_task_rule_inventory_postgres import _database


async def _not_applicable(connection, pool, ids, rule_id):
    await _source(connection, pool, ids, complete=True, not_applicable=True)
    config = json.loads(await connection.fetchval(
        "SELECT task_config FROM qc_task WHERE id=$1::uuid", ids["task"],
    ))
    inventory = parse_task_rule_inventory(config)
    inventory = build_task_rule_inventory(
        ruleset_id=ids["ruleset"], ruleset_version=1, ruleset_snapshot_hash="snapshot-1",
        target_platform="app", channel="workbench", task_config=config,
        rules=[rule for rule in inventory["rules"] if rule["rule_id"] == rule_id],
    )
    config.update(rule_inventory=inventory, ruleset_rule_total=1)
    await connection.execute(
        "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
        ids["task"], json.dumps(config),
    )
    summary = await AggregateInspectionPublicationService(
        repository=PostgresAggregateInspectionPublicationRepository(pool=pool),
    ).prepare(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
    assert summary.not_applicable_count == 1 and summary.published_work_item_count == 0
    return {"inventory_task_config": config, "inventory_target_platform": "app",
            "inventory_ruleset_id": ids["ruleset"], "inventory_channel": "workbench"}


def _facts():
    return CompletionGateFacts(
        exploration_terminal=True, pending_action_count=0, pending_capture_commit_count=0,
        nonterminal_enforced_work_item_count=0, active_session_request_count=0,
        candidate_rule_count=1, terminal_candidate_rule_count=1, background_execution_count=0,
        review_policy_satisfied=True,
    )


async def test_formal_aggregate_not_applicable_closes_inventory_without_fake_judgement():
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        row = await _not_applicable(connection, pool, ids, rules["aggregate"])
        facts = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert facts.gate_passed
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"], agent_run_id=ids["run"],
        )
        assert len(snapshot["aggregate_dispositions"]) == 1
        assert snapshot["aggregate_dispositions"][0]["rule_id"] == rules["aggregate"]
        for table in ("qc_aggregate_inspection_work_item", "qc_rule_execution"):
            assert await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE task_id=$1::uuid", ids["task"],
            ) == 0


@pytest.mark.parametrize("mode", [
    "old_run", "old_epoch", "contract", "scope", "scope_key", "cancel", "task_canceled",
    "session_epoch", "session_active",
    "needs_review", "evidence_insufficient", "authority", "plan_hash", "partial_scope",
])
async def test_untrusted_or_incomplete_aggregate_disposition_cannot_complete_inventory(mode):
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        row = await _not_applicable(connection, pool, ids, rules["aggregate"])
        if mode == "old_run":
            other = str(uuid4())
            await connection.execute(
                """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
                   VALUES($1::uuid,$2::uuid,'main','旧主流程','done')""", other, ids["task"],
            )
            await connection.execute(
                """UPDATE qc_task SET finalization_agent_run_id=$2::uuid
                   WHERE id=$1::uuid""", ids["task"], other,
            )
        elif mode == "old_epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"],
            )
        elif mode == "contract":
            await connection.execute(
                """UPDATE qc_aggregate_inspection_candidate SET rule_contract_hash='wrong-contract'
                   WHERE task_id=$1::uuid""", ids["task"],
            )
        elif mode == "scope":
            await connection.execute(
                """UPDATE qc_aggregate_inspection_candidate SET scope='journey'
                   WHERE task_id=$1::uuid""", ids["task"],
            )
        elif mode == "scope_key":
            await connection.execute(
                """UPDATE qc_aggregate_inspection_candidate SET scope_key='official_group_entry'
                   WHERE task_id=$1::uuid""", ids["task"],
            )
        elif mode == "cancel":
            await connection.execute(
                """UPDATE qc_agent_run SET output_context='{"cancel_requested":true}'
                   WHERE id=$1::uuid""", ids["run"],
            )
        elif mode == "task_canceled":
            await connection.execute(
                "UPDATE qc_task SET status='canceled' WHERE id=$1::uuid", ids["task"],
            )
        elif mode == "session_epoch":
            await connection.execute(
                "UPDATE qc_surface_session SET finalization_epoch=2 WHERE task_id=$1::uuid",
                ids["task"],
            )
        elif mode == "session_active":
            await connection.execute(
                """UPDATE qc_surface_session SET status='active',lost_at=NULL,failure_reason=NULL
                   WHERE task_id=$1::uuid""", ids["task"],
            )
        elif mode in {"needs_review", "evidence_insufficient"}:
            await connection.execute(
                "UPDATE qc_aggregate_inspection_candidate SET status=$2 WHERE task_id=$1::uuid",
                ids["task"], mode,
            )
        else:
            plan = json.loads(await connection.fetchval(
                "SELECT rule_scope_plan FROM qc_aggregate_inspection_candidate "
                "WHERE task_id=$1::uuid",
                ids["task"],
            ))
            if mode == "authority":
                plan["authority_hash"] = "sha256:" + "d"*64
            elif mode == "partial_scope":
                plan["unresolved_page_types"] = ["home"]
                plan["matched_not_applicable_page_types"] = []
            material = {key: value for key, value in plan.items() if key != "plan_hash"}
            plan["plan_hash"] = "sha256:" + (
                "d"*64 if mode == "plan_hash" else stable_protocol_hash(material)
            )
            await connection.execute(
                """UPDATE qc_aggregate_inspection_candidate
                   SET rule_scope_plan=$2::jsonb,rule_scope_plan_hash=$3 WHERE task_id=$1::uuid""",
                ids["task"], json.dumps(plan), plan["plan_hash"],
            )
        facts = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert not facts.gate_passed
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"], agent_run_id=ids["run"],
        )
        assert snapshot["aggregate_dispositions"] == []


async def test_real_detection_gap_is_not_hidden_by_not_applicable_candidate():
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        row = await _not_applicable(connection, pool, ids, rules["aggregate"])
        capture_id = await connection.fetchval(
            "SELECT id FROM qc_capture WHERE task_id=$1::uuid", ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(task_id,rule_id,page_capture_id,agent_run_id,
                   execution_status,judge_strategy,judge_input)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'evidence_insufficient','hybrid',
                      '{"finalization_epoch":0}')""",
            ids["task"], rules["aggregate"], capture_id, ids["run"],
        )
        facts = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert not facts.gate_passed


async def test_sealed_source_rejects_session_build_mutation():
    import asyncpg

    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        row = await _not_applicable(connection, pool, ids, rules["aggregate"])
        with pytest.raises(asyncpg.RaiseError):
            async with connection.transaction():
                await connection.execute(
                    """UPDATE qc_surface_session SET platform_build_ref='app-build:wrong'
                       WHERE task_id=$1::uuid""", ids["task"],
                )
        assert (await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )).gate_passed


async def test_all_aggregate_not_applicable_is_ready_for_normal_gate_before_timeout():
    """全部聚合处置为正式 NA 时，同轮可完成门禁，无需伪造 Work 或 Judge。"""
    from services.api.app.application.task_finalization import EvaluateCompletionGateCommand
    from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _read_completion_gate_facts,
        build_default_task_finalization_service,
    )

    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        await _not_applicable(connection, pool, ids, rules["aggregate"])
        capture = await connection.fetchrow(
            "SELECT id,surface_session_id FROM qc_capture WHERE task_id=$1::uuid", ids["task"],
        )
        await connection.execute(
            """UPDATE qc_app_inspection_publication_entry
               SET status='not_applicable',finished_at=now()
               WHERE publication_id IN (
                   SELECT id FROM qc_app_inspection_publication WHERE task_id=$1::uuid)""",
            ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,
                   source_capture_id,protocol_version,payload_hash,work_payload,status,
                   deadline,finished_at)
               VALUES($1,'exploration',$2::uuid,$3,'na-gate-test',$4::uuid,
                   'exploration-work.v1','test','{}','succeeded',now(),now())""",
            f"na-gate-{ids['task']}", ids["task"], capture["surface_session_id"], capture["id"],
        )
        await connection.execute(
            """UPDATE qc_outbox_event SET status='published',published_at=now()
               WHERE task_id=$1::uuid""", ids["task"],
        )
        command = EvaluateCompletionGateCommand(
            task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
            expected_row_version=0, execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
        )
        _, facts = await _read_completion_gate_facts(connection, command)
        assert facts.gate_passed, facts.block_reasons
        assert await build_default_task_finalization_service(
            pool=pool,
        ).app_completion_ready_before_timeout(command)


@pytest.mark.parametrize("gate_passed", [True, False])
async def test_sealed_report_reads_previous_evidence_epoch_only_with_bound_passed_gate(gate_passed):
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _insert_gate_snapshot,
    )

    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        await _not_applicable(connection, pool, ids, rules["aggregate"])
        facts = _facts()
        if not gate_passed:
            from dataclasses import replace

            facts = replace(facts, pending_action_count=1)
        gate = build_completion_gate_snapshot(
            task_id=ids["task"], finalization_epoch=1, evaluation_sequence=1,
            evaluated_at=datetime.now(UTC), facts=facts,
        )
        await _insert_gate_snapshot(connection, gate)
        await connection.execute(
            """UPDATE qc_task SET status='done',finalization_state='sealed',finalization_epoch=1,
                   finalization_agent_run_id=NULL,completion_gate_snapshot_id=$2,sealed_at=now(),
                   execution_status='completed',quality_outcome='pass'
               WHERE id=$1::uuid""", ids["task"], gate.gate_snapshot_id,
        )
        await connection.execute(
            "UPDATE qc_surface_session SET finalization_epoch=1 WHERE task_id=$1::uuid",
            ids["task"],
        )
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"], agent_run_id=ids["run"],
        )
        assert len(snapshot["aggregate_dispositions"]) == (1 if gate_passed else 0)
