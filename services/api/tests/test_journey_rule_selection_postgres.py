"""隔离 PostgreSQL 证明规则局部选择与完整流程门禁各自封存、互不裁剪。"""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.api.app.application.aggregate_inspection_execution import AggregateInspectionExecutor
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver
from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.domain.exploration import build_exploration_work
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.db.journey_input_repository import (
    load_journey_judge_input,
    load_journey_selection,
)
from services.api.app.infrastructure.db.journey_report_binding import load_journey_report_binding
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只升级显式隔离库
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_journey_consumption_postgres import definition


def plan_definition():
    payload = definition()
    other = deepcopy(payload["checkpoints"][0])
    other["id"] = "other"
    payload["checkpoints"].append(other)
    return payload


def configure_selection(payload, *, second_rule=False, invalid_other=False, legacy=False):
    async def configure(connection, ids):
        definition = parse_journey_definition(payload)
        selectors = [(ids["rule"], "ready")]
        if second_rule:
            ids["other_rule"] = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,"
                "check_point) VALUES($1::uuid,$2::uuid,'OTHER-1','另一检查点规则','text',"
                "'rule_engine','detail')", ids["other_rule"], ids["ruleset"],
            )
            selectors.append((ids["other_rule"], "other"))
        rows = []
        for rule_id, checkpoint_id in selectors:
            capability = {"evidence_input_scope": "journey",
                          "evidence_scope_key": "journey_observation"}
            if not legacy:
                capability["journey_rule_contract"] = {
                    "definition_id": definition.definition_id, "version": 1,
                    "content_hash": definition.content_hash,
                    "workflow_skill": "h5-player-workflow", "scenario_key": "manual",
                    "evidence_conditions": [{"checkpoint_id": checkpoint_id, "requirements": [
                        {"role": "after_pause", "artifact_types": ["screenshot"]},
                    ]}],
                }
            # 复用原夹具角色名，规则只选择已发布义务中实际提供的截图。
            if not legacy:
                capability["journey_rule_contract"]["evidence_conditions"][0]["requirements"][0][
                    "role"
                ] = definition.checkpoints[0].evidence_requirements[0].role
                if invalid_other and rule_id != ids["rule"]:
                    capability["journey_rule_contract"]["evidence_conditions"][0][
                        "checkpoint_id"
                    ] = "unregistered"
            await connection.execute(
                "UPDATE qc_rule SET judge_config=$2::jsonb WHERE id=$1::uuid",
                rule_id, json.dumps({"capability_config": capability}),
            )
            row = dict(await connection.fetchrow(
                "SELECT id::text AS rule_id,rule_code,category,executor_type,severity,check_point,"
                "input_contract,judge_config,manual_review_policy,steps,applicable_page_types "
                "FROM qc_rule WHERE id=$1::uuid", rule_id,
            ))
            for key in ("input_contract", "judge_config", "manual_review_policy", "steps",
                        "applicable_page_types"):
                if isinstance(row[key], str):
                    row[key] = json.loads(row[key])
            rows.append(row)
        channel = await connection.fetchval("SELECT channel FROM qc_task WHERE id=$1::uuid",
                                            ids["task"])
        config = {"workflow_version": "v2", "execution_mode": "full_inspection"}
        config["rule_inventory"] = build_task_rule_inventory(
            ruleset_id=ids["ruleset"], ruleset_version=1, ruleset_snapshot_hash="selection-rules",
            target_platform="h5", channel=channel, task_config=config, rules=rows,
        )
        config["ruleset_rule_total"] = len(rows)
        await connection.execute("UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
                                 ids["task"], json.dumps(config))
        return tuple(sorted(item["contract_hash"] for item in config["rule_inventory"]["rules"]))
    return configure


async def attach_ready_material(connection, turn):
    metadata = {
        "finalization_epoch": 0, "page_state": "stable",
        "pipeline_metrics": {"page_inspection_ready": True},
        "presentation": {"protocol_version": "presentation-stack.v1",
                         "topmost_layer_kind": "page", "base_page_visibility": "visible",
                         "blocking_scope": "none", "mutation_mode": "natural"},
    }
    for capture in (turn.coordination.before_capture, turn.coordination.after_capture):
        await connection.execute("UPDATE qc_capture SET metadata=$2::jsonb WHERE id=$1::uuid",
                                 capture.capture_id, json.dumps(metadata))
        await append_quality(connection, capture.capture_id, "pass", 2)


def snapshots():
    return SimpleNamespace(read_ui_snapshot=AsyncMock(side_effect=[
        sample(paused=False)["snapshot"], sample()["snapshot"],
    ]))


async def bind_checkpoint(connection, repo, ids, work, context, decision, *, sequence=2):
    repo._observer = H5CheckpointObserver(
        snapshots=snapshots(), plans=PostgresJourneyDefinitionRepository(pool=repo._pool),
    )
    assert await repo.reserve(work=work, context=context, decision=decision) == "new"
    turn = await save_receipt(connection, ids, work, decision, command_sequence=sequence)
    await attach_ready_material(connection, turn)
    assert await repo.bind(work=work, context=context, turn=turn)
    return turn


async def complete_other_checkpoint(connection, repo, ids, work, context, decision):
    follow = build_exploration_work(
        task_id=work.task_id, session_id=work.session_id, source_capture_id=work.source_capture_id,
        branch_id="other-checkpoint", objective=work.objective, surface_kind=work.surface_kind,
        frontier_snapshot_id=work.frontier_snapshot_id, frontier=work.frontier,
        boundaries=work.boundaries, budget=work.budget, deadline=work.deadline,
    )
    await connection.execute(
        "INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,"
        "source_capture_id,protocol_version,payload_hash,work_payload,status,deadline,"
        "lease_owner,lease_token,lease_expires_at,started_at) VALUES($1,'exploration',"
        "$2::uuid,$3,$4,$5::uuid,'exploration-work.v1','fixture',$6::jsonb,'running',"
        "$7,'selection-test',gen_random_uuid(),$7,now())", follow.work_id, follow.task_id,
        follow.session_id, follow.branch_id, follow.source_capture_id,
        json.dumps(follow.to_dict()), follow.deadline,
    )
    next_context = {**context, "attempt_id": "other-" + str(uuid4()), "checkpoint_id": "other",
                    "work_id": follow.work_id}
    next_decision = replace(decision, intent_id="other-" + str(uuid4()),
                            idempotency_key=str(uuid4()))
    return await bind_checkpoint(connection, repo, ids, follow, next_context, next_decision,
                                 sequence=3)


@pytest.mark.parametrize("complete_other", [False, True])
async def test_local_rule_and_independent_complete_plan_gate(complete_other):
    payload = plan_definition()
    async with case(checkpoint_payload=payload, configure=configure_selection(payload)) as (
        connection, repo, ids, work, context, decision,
    ):
        await bind_checkpoint(connection, repo, ids, work, context, decision)
        if complete_other:
            await complete_other_checkpoint(connection, repo, ids, work, context, decision)
        await connection.execute(
            "UPDATE qc_task SET status='review',finalization_state='session_draining',"
            "exploration_draining_at=now(),session_draining_at=now(),"
            "finalization_agent_run_id=$2::uuid WHERE id=$1::uuid", ids["task"], ids["run"],
        )
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=repo._pool),
        )
        summary = await publisher.prepare(task_id=ids["task"], agent_run_id=ids["run"],
                                          finalization_epoch=0)
        assert summary.published_work_item_count == 1
        frozen = json.loads(await connection.fetchval(
            "SELECT contract_payload FROM qc_journey_input_contract WHERE task_id=$1::uuid",
            ids["task"],
        ))
        assert frozen["schema_version"] == "journey-input-contract.v2"
        assert frozen["rule_selection"]["checkpoint_ids"] == ["ready"]
        assert len(frozen["plan"]["checkpoints"]) == 2
        queued = await connection.fetchrow(
            "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
            "WHERE task_id=$1::uuid", ids["task"],
        )
        consumer = AggregateInspectionExecutor(
            repository=PostgresAggregateInspectionExecutionRepository(
                pool=repo._pool, snapshot_reader=snapshots(),
            ), lease_owner="selection-test", lease_seconds=30, retry_delay_seconds=1,
        )
        assert await consumer.execute_wakeup(work_item_id=queued["id"], task_id=ids["task"],
                                             message_version=queued["row_version"])
        report = await load_journey_report_binding(connection, task_id=ids["task"],
                                                   agent_run_id=ids["run"], epoch=0)
        assert report["results"][0]["execution_status"] == "pass"
        assert report["gap_count"] == (0 if complete_other else 1)
        assert len(report["results"][0]["checkpoints"]) == 1


@pytest.mark.parametrize("invalid_other", [False, True])
async def test_rule_selections_keep_other_rules_missing_material_independent(invalid_other):
    payload = plan_definition()
    async with case(checkpoint_payload=payload,
                    configure=configure_selection(payload, second_rule=True,
                                                  invalid_other=invalid_other)) as (
        connection, repo, ids, work, context, decision,
    ):
        try:
            await bind_checkpoint(connection, repo, ids, work, context, decision)
            config = json.loads(await connection.fetchval(
                "SELECT task_config FROM qc_task WHERE id=$1::uuid", ids["task"],
            ))
            selections = {}
            for rule in config["rule_inventory"]["rules"]:
                selections[rule["rule_id"]] = await load_journey_selection(
                    connection, task_id=ids["task"], agent_run_id=ids["run"], epoch=0,
                    rule_hash=rule["contract_hash"],
                )
            chosen = selections[ids["rule"]]["contract"]["rule_selection"]["checkpoint_ids"]
            assert chosen == ["ready"]
            assert selections[ids["other_rule"]] is None
        finally:
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", ids["other_rule"])


async def test_existing_v1_input_is_read_with_original_contract_and_hash():
    from services.api.app.domain.evidence import EvidenceInputSet

    payload = definition()
    async with case(checkpoint_payload=payload,
                    configure=configure_selection(payload)) as (
        connection, repo, ids, work, context, decision,
    ):
        await bind_checkpoint(connection, repo, ids, work, context, decision)
        await connection.execute(
            "UPDATE qc_task SET status='review',finalization_state='session_draining',"
            "exploration_draining_at=now(),session_draining_at=now(),"
            "finalization_agent_run_id=$2::uuid WHERE id=$1::uuid", ids["task"], ids["run"],
        )
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=repo._pool),
        )
        async def legacy_selection(*args, **kwargs):
            return await load_journey_selection(*args, **{**kwargs, "legacy_full_plan": True})

        with patch(
            "services.api.app.infrastructure.db.journey_input_repository.load_journey_selection",
            legacy_selection,
        ):
            await publisher.prepare(task_id=ids["task"], agent_run_id=ids["run"],
                                    finalization_epoch=0)
        row = await connection.fetchrow(
            "SELECT inputs.input_set_payload,contract.contract_payload,contract.selection_hash "
            "FROM qc_evidence_input_set inputs JOIN qc_journey_input_contract contract "
            "USING(input_set_id) WHERE contract.task_id=$1::uuid", ids["task"],
        )
        original_contract = json.loads(row["contract_payload"])
        assert original_contract["schema_version"] == "journey-input-contract.v1"
        input_set = EvidenceInputSet.from_dict(json.loads(row["input_set_payload"]))
        read = await load_journey_judge_input(connection, input_set=input_set)
        assert read == original_contract
        assert await connection.fetchval(
            "SELECT selection_hash FROM qc_journey_input_contract WHERE task_id=$1::uuid",
            ids["task"],
        ) == row["selection_hash"]
        retried = await publisher.prepare(task_id=ids["task"], agent_run_id=ids["run"],
                                           finalization_epoch=0)
        assert retried.published_work_item_count == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_journey_input_contract WHERE task_id=$1::uuid", ids["task"],
        ) == 1
