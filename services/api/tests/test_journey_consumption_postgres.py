"""隔离数据库贯通精确选择、InputSet、聚合 Judge 与报告绑定；不运行原站。"""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.aggregate_inspection_execution import (
    AggregateInspectionExecutor,
)
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
    build_aggregate_rule_source,
)
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.journey_input_repository import load_journey_selection
from services.api.app.infrastructure.db.journey_report_binding import (
    bind_journey_report,
    load_journey_report_binding,
)
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition


async def configure(connection, ids):
    config = {
        "capability_config": {
            "evidence_input_scope": "journey",
            "evidence_scope_key": "journey_observation",
        }
    }
    await connection.execute(
        "UPDATE qc_rule SET judge_config=$2::jsonb WHERE id=$1::uuid",
        ids["rule"],
        json.dumps(config),
    )
    await connection.execute(
        "UPDATE qc_ruleset SET snapshot_hash='controlled-rules' WHERE id=$1::uuid", ids["ruleset"]
    )
    row = dict(
        await connection.fetchrow(
            "SELECT id::text AS rule_id,rule_code,category,executor_type,severity,check_point,"
            "input_contract,judge_config,manual_review_policy,steps,applicable_page_types "
            "FROM qc_rule WHERE id=$1::uuid",
            ids["rule"],
        )
    )
    for key in (
        "input_contract",
        "judge_config",
        "manual_review_policy",
        "steps",
        "applicable_page_types",
    ):
        if isinstance(row[key], str):
            row[key] = json.loads(row[key])
    return (build_inspection_rule_contract_hash(build_aggregate_rule_source(row).rule_payload),)


def definition():
    payload = checkpoint_definition("controlled-" + str(uuid4()))
    payload["checkpoints"] = [
        {
            **payload["checkpoints"][0],
            "entry_condition": {"playing": True},
            "expected": {"paused": True},
            "exit_condition": {"observation_complete": True},
            "evidence_requirements": [
                {"role": "after_pause", "artifact_types": ["screenshot", "action_receipt"]}
            ],
        }
    ]
    return payload


async def test_published_rule_editor_roundtrip_uses_real_repository_and_api():
    from services.api.app.api.v1.rules import RuleBusinessPayload, patch_rule
    from services.api.app.application.h5_shortflow_read import definition_summary
    from services.api.app.infrastructure.db.rule_repository import PostgresRuleRepository
    from services.api.tests.test_h5_shortflow_activation import definition_payload

    payload = definition_payload()
    payload["definition_id"] = "editor-" + str(uuid4())
    async with case(checkpoint_payload=payload) as (connection, checkpoint_repo, ids, *_):
        await connection.execute(
            "UPDATE qc_rule SET status='disabled',"
            "applicable_page_types='[\"video_detail\"]'::jsonb,"
            "input_contract='{\"target_clients\":[\"H5\"]}'::jsonb WHERE id=$1::uuid",
            ids["rule"],
        )
        repo = PostgresRuleRepository(pool=checkpoint_repo._pool)
        published = await repo.get_journey_definition(
            definition_id=payload["definition_id"], version=1,
        )
        summary = definition_summary(published)
        configured = {key: summary[key] for key in (
            "definition_id", "version", "content_hash", "workflow_skill", "evidence_conditions",
        )} | {"scenario_key": "pause"}
        before = await repo.get_rule(ids["rule"])
        response = await patch_rule(
            ids["rule"], RuleBusinessPayload(
                name=before["name"], check_point=before["check_point"],
                applicable_page_types=before["applicable_page_types"],
                evidence_input_scope="journey", journey_rule_contract=configured,
            ), None, repo,
        )
        loaded = await repo.get_rule(ids["rule"])
        assert response.judge_config["capability_config"]["journey_rule_contract"] == configured
        assert loaded["judge_config"]["capability_config"]["journey_rule_contract"] == configured
        assert loaded["input_contract"] == before["input_contract"]
        assert loaded["manual_review_policy"] == before["manual_review_policy"]


@pytest.mark.parametrize("outcome", ["pass", "fail"])
async def test_exact_journey_input_judge_and_report_consume_real_rows(outcome):
    async with case(checkpoint_payload=definition(), configure=configure) as (
        connection,
        checkpoint_repo,
        ids,
        work,
        context,
        decision,
    ):
        await checkpoint_repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await checkpoint_repo.bind(work=work, context=context, turn=turn)
        metadata = {
            "finalization_epoch": 0,
            "page_state": "stable",
            "pipeline_metrics": {"page_inspection_ready": True},
            "presentation": {
                "protocol_version": "presentation-stack.v1",
                "topmost_layer_kind": "page",
                "base_page_visibility": "visible",
                "blocking_scope": "none",
                "mutation_mode": "natural",
            },
        }
        await connection.execute(
            "UPDATE qc_capture SET metadata=$2::jsonb WHERE task_id=$1::uuid",
            ids["task"],
            json.dumps(metadata),
        )
        for capture in (turn.coordination.before_capture, turn.coordination.after_capture):
            await append_quality(connection, capture.capture_id, "pass", 2)
        await connection.execute(
            "UPDATE qc_task SET status='review',finalization_state='session_draining',"
            "exploration_draining_at=now(),session_draining_at=now(),"
            "finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        pool = checkpoint_repo._pool
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=pool)
        )
        summary = await publisher.prepare(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
        )
        assert summary.published_work_item_count == 1
        again = await publisher.prepare(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
        )
        assert again.published_work_item_count == 0
        queued = await connection.fetchrow(
            "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        before = sample(paused=False)["snapshot"]
        after = sample(paused=outcome != "fail")["snapshot"]
        reader = SimpleNamespace(read_ui_snapshot=AsyncMock(side_effect=[before, after]))
        consumer = AggregateInspectionExecutor(
            repository=PostgresAggregateInspectionExecutionRepository(
                pool=pool, snapshot_reader=reader
            ),
            lease_owner="controlled-journey",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert await consumer.execute_wakeup(
            work_item_id=queued["id"], task_id=ids["task"], message_version=queued["row_version"]
        )
        result = await connection.fetchrow(
            "SELECT execution_status,judge_output FROM qc_rule_execution "
            "WHERE evidence_input_set_id IS NOT NULL AND task_id=$1::uuid",
            ids["task"],
        )
        assert result["execution_status"] == outcome
        binding = await load_journey_report_binding(
            connection, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
        )
        assert binding["gap_count"] == 0
        assert binding["results"][0]["execution_status"] == outcome
        with pytest.raises(ValueError):
            await bind_journey_report(
                connection, {}, task_id=ids["task"], agent_run_id=str(uuid4()), epoch=0
            )
        with pytest.raises(ValueError):
            await bind_journey_report(
                connection, {}, task_id=ids["task"], agent_run_id=ids["run"], epoch=1
            )
        report = {}
        await bind_journey_report(
            connection, report, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
        )
        assert report["h5_journeys"] == binding
        from services.api.app.application.h5_journey_result_read import project_live_journey_result
        from services.api.app.application.report_insight import _bound_final_snapshot_insight
        from services.api.app.infrastructure.db.task_insight_repository import (
            PostgresTaskInsightRepository,
        )

        rows = await PostgresTaskInsightRepository(pool=pool).list_rule_execution_rows(
            task_id=ids["task"], agent_run_id=None,
        )
        row = next(item for item in rows if item["evidence_input_set_id"])
        projected = project_live_journey_result(row)
        assert projected is not None
        assert (
            projected["checkpoints"][0]["bindings"]
            == binding["results"][0]["checkpoints"][0]["bindings"]
        )
        assert project_live_journey_result({**row, "evidence_input_set_id": "foreign"}) is None
        assert project_live_journey_result({**row, "rule_id": str(uuid4())}) is None
        frozen = _bound_final_snapshot_insight({
            "task": {"task_id": ids["task"], "status": "done"}, "agent_run_id": ids["run"],
            "completion_gate": {"finalization_epoch": 0}, "h5_journeys": binding,
            "rule_results": [{"execution_id": row["execution_id"], "execution_status": outcome,
                              "evidence_input_set_id": row["evidence_input_set_id"],
                              "evidence_artifact_ids": row["evidence_artifact_ids"]}],
        }, target_platform="h5")
        assert frozen["rule_items"][0]["journey_result"] == projected
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
        await append_quality(connection, turn.coordination.after_capture.capture_id, "fail", 3)
        stale = await load_journey_report_binding(
            connection, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
        )
        assert stale["gap_count"] == 1
        with pytest.raises(ValueError):
            await bind_journey_report(
                connection, {}, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
            )
        # 封存输入不回写，晚到失败使原结果不可用于新正式报告。
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_input_contract WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
        with pytest.raises(asyncpg.RaiseError):
            await connection.execute(
                "UPDATE qc_journey_input_contract SET selection_hash='forged' "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
        migration = importlib.import_module(
            "services.api.migrations.versions.20261016_0137_journey_inputs"
        )
        statements = []
        with patch.object(migration, "op", SimpleNamespace(execute=statements.append)):
            migration.downgrade()
        # 使用迁移原 SQL 验证有输入时拒绝回滚，异常事务不能抹掉已封存证据。
        with pytest.raises(asyncpg.RaiseError, match="禁止回滚抹去"):
            async with connection.transaction():
                for statement in statements:
                    await connection.execute(statement)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_input_contract WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 1
        )


async def test_missing_frozen_rule_mapping_cannot_select_other_rule_or_partial_checkpoint():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        assert (
            await load_journey_selection(
                connection,
                task_id=ids["task"],
                agent_run_id=ids["run"],
                epoch=0,
                rule_hash="unbound",
            )
            is None
        )
