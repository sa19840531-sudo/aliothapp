"""隔离 PG 验证只读结果关联；动作与观测是显式夹具，不是原站采集验收。"""

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.aggregate_inspection_execution import AggregateInspectionExecutor
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.h5_shortflow_read_repository import (
    PostgresH5ShortflowReadRepository,
)
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_call_audit_postgres import scope
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_journey_consumption_postgres import configure, definition
from services.api.tests.test_journey_rule_selection_postgres import (
    configure_selection,
    plan_definition,
)


@asynccontextmanager
async def result_case(*, outcome="pass", execute=True, subset=False):
    payload = plan_definition() if subset else definition()
    if subset:
        # 所选 ready 位于完整计划第二项，避免只按数组位置碰巧关联正确。
        payload["checkpoints"].reverse()
    configured = configure_selection(payload) if subset else configure
    async with case(checkpoint_payload=payload, configure=configured) as fixture:
        connection, repo, ids, work, context, decision = fixture
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
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
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=repo._pool)
        )
        summary = await publisher.prepare(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert summary.published_work_item_count == 1
        if execute:
            queued = await connection.fetchrow(
                "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
            before, after = (
                sample(paused=False)["snapshot"],
                sample(paused=outcome != "fail")["snapshot"],
            )
            if outcome == "evidence_insufficient":
                after["elements"][0]["media"].pop("paused")
            executor = AggregateInspectionExecutor(
                repository=PostgresAggregateInspectionExecutionRepository(
                    pool=repo._pool,
                    snapshot_reader=SimpleNamespace(
                        read_ui_snapshot=AsyncMock(side_effect=[before, after])
                    ),
                ),
                lease_owner="shortflow-read-fixture",
                lease_seconds=30,
                retry_delay_seconds=1,
            )
            assert await executor.execute_wakeup(
                work_item_id=queued["id"],
                task_id=ids["task"],
                message_version=queued["row_version"],
            )
        yield fixture, turn


async def read_snapshot(repo, ids):
    return await PostgresH5ShortflowReadRepository(pool=repo._pool).read(
        task_id=ids["task"],
        access_scope=scope(ids),
    )


async def unchanged_facts(connection, task_id):
    return dict(
        await connection.fetchrow(
            """SELECT task.row_version,task.finalization_epoch,task.finalization_state,
                  task.completion_gate_snapshot_id::text,
                  (SELECT count(*) FROM qc_rule_execution WHERE task_id=task.id) AS executions,
                  (SELECT count(*) FROM qc_evidence_input_set WHERE task_id=task.id) AS inputs,
                  (SELECT count(*) FROM qc_action_execution WHERE task_id=task.id) AS actions,
                  (SELECT count(*) FROM qc_v2_work_item WHERE task_id=task.id) AS works,
                  (SELECT count(*) FROM qc_journey_checkpoint_attempt
                     WHERE task_id=task.id) AS attempts
           FROM qc_task task WHERE task.id=$1::uuid""",
            task_id,
        )
    )


@pytest.mark.parametrize("outcome", ["pass", "fail", "evidence_insufficient"])
async def test_saved_rule_items_link_exact_attempt_and_originals_without_writes(outcome):
    async with result_case(outcome=outcome) as (fixture, turn):
        connection, repo, ids, work, context, decision = fixture
        before = await unchanged_facts(connection, ids["task"])
        snapshot = await read_snapshot(repo, ids)
        plan = snapshot["plans"][0]
        assert plan["rule_results_status"] == "available"
        result = plan["rule_results"][0]
        assert result["execution_status"] == outcome
        assert result["linkage_status"] == "linked"
        checkpoint = result["checkpoints"][0]
        assert checkpoint["checkpoint_id"] == context["checkpoint_id"]
        assert checkpoint["status"] == outcome
        assert {item["capture_id"] for item in checkpoint["evidence_refs"]} == {
            turn.coordination.before_capture.capture_id,
            turn.coordination.after_capture.capture_id,
        }
        assert all(
            item["attempt_id"] == context["attempt_id"] for item in checkpoint["evidence_refs"]
        )
        assert all(item["session_id"] == work.session_id for item in checkpoint["evidence_refs"])
        assert all(item["manifest_version"] == 2 for item in checkpoint["evidence_refs"])
        if outcome == "evidence_insufficient":
            assert "measurement:paused" in checkpoint["missing"]
            assert result["review_status"] == "pending"
        assert not {
            "judge_output",
            "wrapper_payload",
            "contract_payload",
            "storage_path",
            "metadata",
        } & set(result)
        assert "storage_path" not in json.dumps(snapshot)
        assert await read_snapshot(repo, ids) == snapshot
        assert await unchanged_facts(connection, ids["task"]) == before
        assert (
            await PostgresH5ShortflowReadRepository(pool=repo._pool).read(
                task_id=ids["task"],
                access_scope=scope(ids, authorized=False),
            )
            is None
        )


async def test_frozen_material_without_execution_does_not_become_rule_pass():
    async with result_case(execute=False) as (fixture, _):
        connection, repo, ids, *_ = fixture
        snapshot = await read_snapshot(repo, ids)
        plan = snapshot["plans"][0]
        assert plan["rule_results_status"] == "unavailable"
        result = plan["rule_results"][0]
        assert result["linkage_status"] == "missing_result"
        assert result["execution_status"] is None and result["checkpoints"] == []


@pytest.mark.parametrize("outcome", ["pass", "fail", "evidence_insufficient"])
async def test_subset_links_second_checkpoint_through_public_api_and_keeps_full_gate_gap(outcome):
    import httpx

    from services.api.app.api.v2 import h5_shortflows
    from services.api.app.infrastructure.db.journey_report_binding import (
        load_journey_report_binding,
    )
    from services.api.app.main import create_app

    async with result_case(subset=True, outcome=outcome) as (fixture, turn):
        connection, repo, ids, _, context, _ = fixture
        before = await unchanged_facts(connection, ids["task"])
        snapshot = await read_snapshot(repo, ids)
        plan = snapshot["plans"][0]
        assert [item["checkpoint_id"] for item in plan["checkpoints"]] == ["other", "ready"]
        assert plan["rule_results_status"] == "available"
        result = plan["rule_results"][0]
        assert result["linkage_status"] == "linked"
        assert result["execution_status"] == outcome
        assert [item["checkpoint_id"] for item in result["checkpoints"]] == ["ready"]
        refs = result["checkpoints"][0]["evidence_refs"]
        assert {item["attempt_id"] for item in refs} == {context["attempt_id"]}
        assert {item["capture_id"] for item in refs} == {
            turn.coordination.before_capture.capture_id,
            turn.coordination.after_capture.capture_id,
        }
        app = create_app()
        app.dependency_overrides[h5_shortflows._get_repository] = lambda: (
            PostgresH5ShortflowReadRepository(pool=repo._pool)
        )
        app.dependency_overrides[h5_shortflows.require_task_read_access] = lambda: scope(ids)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated.test",
        ) as api:
            response = await api.get(f"/api/v2/tasks/{ids['task']}/h5-shortflows")
        assert response.status_code == 200, response.text
        assert response.json() == snapshot
        report = await load_journey_report_binding(connection, task_id=ids["task"],
                                                   agent_run_id=ids["run"], epoch=0)
        assert report["gap_count"] == 1
        assert await unchanged_facts(connection, ids["task"]) == before


async def rewrite_result(connection, task_id, output):
    """同步更新测试结果链摘要，避免篡改反例只被外围摘要不一致挡住。"""
    payload, digest = json.dumps(output), stable_protocol_hash(output)
    await connection.execute(
        "UPDATE qc_rule_execution SET judge_output=$2::jsonb WHERE task_id=$1::uuid",
        task_id, payload,
    )
    for table in ("qc_aggregate_inspection_work_item", "qc_aggregate_inspection_attempt"):
        await connection.execute(
            f"UPDATE {table} SET result_payload=$2::jsonb,result_hash=$3 WHERE task_id=$1::uuid",
            task_id, payload, digest,
        )
    await connection.execute(
        "UPDATE qc_observation_packet SET packet_payload=$2::jsonb "
        "WHERE task_id=$1::uuid AND aggregate_work_item_id IS NOT NULL",
        task_id, json.dumps(output["wrapper_payload"]),
    )


@pytest.mark.parametrize("fault", ["unselected", "duplicate", "replace", "binding", "version"])
async def test_subset_rejects_unselected_items_or_changed_material_despite_matching_hashes(fault):
    async with result_case(subset=True) as (fixture, turn):
        connection, repo, ids, *_ = fixture
        assert (await read_snapshot(repo, ids))["plans"][0]["rule_results_status"] == "available"
        if fault == "version":
            await append_quality(connection, turn.coordination.after_capture.capture_id, "pass", 3)
        else:
            output = json.loads(await connection.fetchval(
                "SELECT judge_output FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
            ))
            checkpoints = output["wrapper_payload"]["journey"]["checkpoints"]
            if fault in {"unselected", "duplicate"}:
                extra = deepcopy(checkpoints[0])
                if fault == "unselected":
                    extra["checkpoint_id"] = "other"
                checkpoints.append(extra)
            elif fault == "replace":
                checkpoints[0]["checkpoint_id"] = "other"
            else:
                checkpoints[0]["bindings"][0]["capture_id"] = str(uuid4())
            await rewrite_result(connection, ids["task"], output)
        before = await unchanged_facts(connection, ids["task"])
        snapshot = await read_snapshot(repo, ids)
        result = snapshot["plans"][0]["rule_results"][0]
        assert result["linkage_status"] != "linked"
        assert result["execution_status"] is None and result["checkpoints"] == []
        assert await unchanged_facts(connection, ids["task"]) == before


async def test_subset_read_rejects_execution_linked_to_another_real_input_set():
    async with result_case(subset=True) as (fixture, _):
        connection, repo, ids, *_ = fixture
        async with result_case(subset=True, execute=False) as (foreign_fixture, _):
            _, _, foreign_ids, *_ = foreign_fixture
            foreign_input = await connection.fetchval(
                "SELECT input_set_id FROM qc_journey_input_contract WHERE task_id=$1::uuid",
                foreign_ids["task"],
            )
            original_judge_input = await connection.fetchval(
                "SELECT judge_input FROM qc_rule_execution WHERE task_id=$1::uuid",
                ids["task"],
            )
            try:
                # 数据库拒绝跨任务列关联；JSON 中冒用真实外来 InputSet 仍须由只读合同拒绝。
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    await connection.execute(
                        "UPDATE qc_rule_execution SET evidence_input_set_id=$2 "
                        "WHERE task_id=$1::uuid", ids["task"], foreign_input,
                    )
                await connection.execute(
                    "UPDATE qc_rule_execution SET judge_input=jsonb_set(judge_input,"
                    "'{evidence_input_set_id}',to_jsonb($2::text)) WHERE task_id=$1::uuid",
                    ids["task"], foreign_input,
                )
                before = await unchanged_facts(connection, ids["task"])
                snapshot = await read_snapshot(repo, ids)
                result = snapshot["plans"][0]["rule_results"][0]
                assert result["linkage_status"] == "input_mismatch"
                assert result["execution_status"] is None and result["checkpoints"] == []
                assert await unchanged_facts(connection, ids["task"]) == before
            finally:
                await connection.execute(
                    "UPDATE qc_rule_execution SET judge_input=$2::jsonb WHERE task_id=$1::uuid",
                    ids["task"], original_judge_input,
                )


@pytest.mark.parametrize(
    "fault", ["latest_quality", "session", "run", "epoch", "input_hash", "output_plan", "packet"]
)
async def test_changed_material_or_forged_identity_never_reuses_old_pass(fault):
    async with result_case() as (fixture, turn):
        connection, repo, ids, *_ = fixture
        original = await read_snapshot(repo, ids)
        assert original["plans"][0]["rule_results"][0]["execution_status"] == "pass"
        if fault == "latest_quality":
            await append_quality(connection, turn.coordination.after_capture.capture_id, "fail", 3)
        elif fault == "session":
            await connection.execute(
                "UPDATE qc_capture SET surface_session_id='foreign-session' WHERE id=$1::uuid",
                turn.coordination.after_capture.capture_id,
            )
        elif fault == "run":
            foreign_run = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name) "
                "VALUES($1::uuid,$2::uuid,'rule_judge','unrelated-read-fixture')",
                foreign_run,
                ids["task"],
            )
            await connection.execute(
                "UPDATE qc_capture SET agent_run_id=$1::uuid WHERE id=$2::uuid",
                foreign_run,
                turn.coordination.after_capture.capture_id,
            )
        elif fault == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=finalization_epoch+1 WHERE id=$1::uuid",
                ids["task"],
            )
        elif fault == "packet":
            await connection.execute(
                "UPDATE qc_observation_packet SET packet_payload=jsonb_set(packet_payload,"
                "'{scope_key}','\"foreign-packet-scope\"'::jsonb) "
                "WHERE task_id=$1::uuid AND aggregate_work_item_id IS NOT NULL",
                ids["task"],
            )
        elif fault == "input_hash":
            await connection.execute(
                "UPDATE qc_rule_execution SET judge_input=jsonb_set(judge_input,'{input_hash}',"
                "'\"foreign-input\"'::jsonb) WHERE task_id=$1::uuid",
                ids["task"],
            )
        else:
            await connection.execute(
                "UPDATE qc_rule_execution SET judge_output=jsonb_set(judge_output,"
                "'{wrapper_payload,journey,execution_plan_id}','\"foreign-plan\"'::jsonb) "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
        before = await unchanged_facts(connection, ids["task"])
        changed = await read_snapshot(repo, ids)
        result = changed["plans"][0]["rule_results"][0]
        assert result["linkage_status"] != "linked"
        assert result["execution_status"] is None
        assert result["checkpoints"] == []
        assert changed["snapshot_hash"] != original["snapshot_hash"]
        assert await unchanged_facts(connection, ids["task"]) == before


async def test_selected_plan_without_frozen_input_has_explicit_notice_not_material_pass():
    async with case(checkpoint_payload=definition(), configure=configure) as fixture:
        connection, repo, ids, *_ = fixture
        before = await unchanged_facts(connection, ids["task"])
        snapshot = await read_snapshot(repo, ids)
        plan = snapshot["plans"][0]
        assert plan["rule_results_status"] == "unavailable"
        assert plan["rule_results_notice"]
        assert plan["rule_results"] == []
        assert await unchanged_facts(connection, ids["task"]) == before


async def test_public_projection_never_returns_unrelated_raw_output_fields():
    async with result_case() as (fixture, _):
        connection, repo, ids, *_ = fixture
        output = json.loads(
            await connection.fetchval(
                "SELECT judge_output FROM qc_rule_execution WHERE task_id=$1::uuid",
                ids["task"],
            )
        )
        output["private_debug"] = {"authorization": "Bearer test-only-secret"}
        wrapper = output["wrapper_payload"]
        wrapper["internal_document"] = "<html>test-only-original-dom</html>"
        wrapper["journey"]["checkpoints"][0]["measured"] = {
            "storage_path": "test-only-private-storage-path",
        }
        payload = json.dumps(output)
        digest = stable_protocol_hash(output)
        await connection.execute(
            "UPDATE qc_rule_execution SET judge_output=$2::jsonb WHERE task_id=$1::uuid",
            ids["task"],
            payload,
        )
        for table in ("qc_aggregate_inspection_work_item", "qc_aggregate_inspection_attempt"):
            await connection.execute(
                f"UPDATE {table} SET result_payload=$2::jsonb,result_hash=$3 "
                "WHERE task_id=$1::uuid",
                ids["task"],
                payload,
                digest,
            )
        await connection.execute(
            "UPDATE qc_observation_packet SET packet_payload=$2::jsonb "
            "WHERE task_id=$1::uuid AND aggregate_work_item_id IS NOT NULL",
            ids["task"],
            json.dumps(wrapper),
        )
        snapshot = await read_snapshot(repo, ids)
        assert snapshot["plans"][0]["rule_results"][0]["linkage_status"] == "linked"
        text = json.dumps(snapshot)
        assert not any(
            value in text
            for value in (
                "test-only-secret",
                "test-only-original-dom",
                "test-only-private-storage-path",
                "private_debug",
                "internal_document",
            )
        )
