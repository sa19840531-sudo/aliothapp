"""真实多规则 Writer 的部分提交、合同冲突终结及逐条来源追溯。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_rule_evidence_contract import _contract
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_sequence_consumer_pg_support import consume_sequence_with_actual_work
from services.api.tests.web_sequence_runtime_pg_support import (
    prepare_sequence_with_default_notification,
)
from services.api.tests.web_sequence_stable_pg_support import publish_stable_anchor

pytestmark = pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="仅专用隔离 PG")


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_index", [0, 1])
async def test_real_batch_contract_conflict_preserves_written_sources_and_closes_work(
    conflict_index, monkeypatch, stable_current=False, independent_first=None
):
    async with target_database() as (connection, pool, _other, ids):
        rules = [str(uuid4()), str(uuid4())]
        try:
            contract = _contract()
            contract.update(
                accepted_roles=["stable_page", "interaction_sequence", "failure_sequence"],
                minimum_duration_ms=0,
            )
            for index, rule_id in enumerate(rules):
                rule_contract = dict(contract)
                if stable_current and index != conflict_index:
                    rule_contract.update(accepted_roles=["stable_page"], accept_unclassified=False)
                await connection.execute(
                    "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,status,check_point,input_contract,judge_config,manual_review_policy,applicable_page_types,sort_order) "
                    "VALUES($1::uuid,$2::uuid,$3,'批量合同规则','interaction','llm','medium','enabled','按真实材料判断',$4::jsonb,"
                    '\'{"tool_refs":["semantic_rule_judge_tool"]}\',\'{"min_confidence":0.8}\',\'["content_detail"]\',$5)',
                    rule_id,
                    ids["ruleset"],
                    f"t9_batch_{index}",
                    json.dumps({"target_clients": ["WEB"], "web_evidence": rule_contract}),
                    index,
                )
            (
                result,
                published,
                store,
                source,
                notification,
            ) = await prepare_sequence_with_default_notification(
                connection, pool, ids, monkeypatch, interaction=True
            )
            publications = PostgresH5InspectionPublicationRepository(pool=pool)
            _, stable_lease = await publish_stable_anchor(
                pool,
                ids,
                source,
                notification,
                result.ready_capture_id,
                publications,
                monkeypatch,
                defer_work=stable_current,
            )

            async def claim_sequence():
                version = await connection.fetchval(
                    "SELECT row_version FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                    published[0]["publication_id"],
                )
                claimed = await publications.claim(
                    publication_id=published[0]["publication_id"],
                    task_id=ids["task"],
                    message_version=version,
                    lease_owner="t9-fix2",
                    lease_seconds=60,
                )
                assert claimed.lease is not None
                return claimed, await publications.process_web_sequence(lease=claimed.lease)

            if stable_current:
                claim, previous_work_ids = await claim_sequence()
            stable, packets = await consume_sequence_with_actual_work(
                connection,
                pool,
                store,
                ids,
                stable_lease.consumer_agent_run_id,
                monkeypatch,
                conclusion="fail",
            )
            assert stable.completed_count == 1 and len(packets) == (1 if stable_current else 2)
            if independent_first:
                from services.api.app.application.app_inspection_publisher import (
                    AppCaptureInspectionPublisher,
                )
                from services.api.app.application.inspection_work_items import (
                    InspectionWorkItemService,
                )
                from services.api.app.application.task_query import TaskQueryService
                from services.api.app.infrastructure.db.task_repository import (
                    PostgresTaskRepository,
                )
                from services.api.app.infrastructure.db.web_sequence_result_projection import (
                    _scopes,
                )

                await connection.execute(
                    "UPDATE qc_rule SET status='disabled' WHERE id=$1::uuid", rules[conflict_index]
                )
                query = TaskQueryService(repository=PostgresTaskRepository(pool=pool))
                from services.api.app.agents.runtime.business_middlewares import base

                monkeypatch.setattr(base, "_get_task_query_service", lambda: query)
                prepared = await AppCaptureInspectionPublisher(
                    task_query=query
                ).publish_for_capture(
                    task_id=ids["task"],
                    capture_id=result.ready_capture_id,
                    visual_page_type="content_detail",
                    agent_run_id=ids["run"],
                    snapshot_only=True,
                    prepare_only=True,
                )
                assert (
                    len(prepared.prepared_commands) == 1
                    and len(prepared.prepared_commands[0].rules) == 1
                )
                independent = await InspectionWorkItemService(
                    repository=PostgresInspectionWorkItemRepository(pool=pool)
                ).publish(prepared.prepared_commands[0])
                assert await _scopes(connection, independent.work_item_id) == [], (
                    "其他Work中B序列不能授权独立A"
                )
                old_a, old_packets = await consume_sequence_with_actual_work(
                    connection,
                    pool,
                    store,
                    ids,
                    stable_lease.consumer_agent_run_id,
                    monkeypatch,
                    conclusion="fail",
                )
                assert old_a.completed_count == 1 and len(old_packets) == 1
                independent_formal = dict(
                    await connection.fetchrow(
                        "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                        ids["task"],
                        rules[0],
                    )
                )
                independent_attempt = await connection.fetchrow(
                    "SELECT id,result_payload FROM qc_inspection_attempt WHERE work_item_id=$1::uuid",
                    independent.work_item_id,
                )
                assert "web_composite_provenance" not in json.loads(
                    independent_formal["judge_output"]
                )
                if independent_first == "missing":
                    await connection.execute(
                        "UPDATE qc_inspection_work_item_rule SET rule_execution_id=NULL WHERE work_item_id=$1::uuid",
                        independent.work_item_id,
                    )
                elif independent_first == "forged":
                    await connection.execute(
                        "UPDATE qc_inspection_work_item_rule SET observation_packet_id=(SELECT observation_packet_id FROM qc_rule_execution WHERE task_id=$2::uuid AND rule_id=$3::uuid) WHERE work_item_id=$1::uuid",
                        independent.work_item_id,
                        ids["task"],
                        rules[conflict_index],
                    )
                elif independent_first == "different":
                    await connection.execute(
                        "UPDATE qc_rule SET severity='high' WHERE id=$1::uuid",
                        rules[0],
                    )
                await connection.execute(
                    "UPDATE qc_rule SET status='enabled' WHERE id=$1::uuid", rules[conflict_index]
                )
                previous_work_ids = (*previous_work_ids, independent.work_item_id)
            prior = dict(
                await connection.fetchrow(
                    "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                    ids["task"],
                    rules[conflict_index],
                )
            )
            await connection.execute(
                "UPDATE qc_rule SET check_point='新版本完整判定标准' WHERE id=$1::uuid",
                rules[conflict_index],
            )
            if stable_current:
                from services.api.tests.web_sequence_stable_pg_support import (
                    complete_stable_publication,
                )

                await complete_stable_publication(
                    pool, ids, result.ready_capture_id, publications, stable_lease, monkeypatch
                )
                work_ids = [
                    str(row["id"])
                    for row in await connection.fetch(
                        "SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid AND id<>ALL($2::uuid[])",
                        ids["task"],
                        list(previous_work_ids),
                    )
                ]
            else:
                claim, work_ids = await claim_sequence()
            assert len(work_ids) == 1
            work_id = work_ids[0]
            from services.api.app.agents.runtime import inspection_result_writer

            actual_batch_writer = inspection_result_writer.record_rule_executions_for_current_run
            before_write_payloads = []

            async def observe_actual_batch(**kwargs):
                assert len(kwargs["executions"]) == 2
                before_write_payloads.append(
                    await connection.fetchval(
                        "SELECT result_payload FROM qc_inspection_attempt WHERE work_item_id=$1::uuid",
                        work_id,
                    )
                )
                return await actual_batch_writer(**kwargs)

            monkeypatch.setattr(
                inspection_result_writer,
                "record_rule_executions_for_current_run",
                observe_actual_batch,
            )
            consumed, packets = await consume_sequence_with_actual_work(
                connection, pool, store, ids, claim.lease.consumer_agent_run_id, monkeypatch
            )
            assert len(packets) == 2, (
                consumed,
                [
                    dict(row)
                    for row in await connection.fetch(
                        "SELECT w.status,w.last_error_code,a.result_payload FROM qc_inspection_work_item w LEFT JOIN qc_inspection_attempt a ON a.work_item_id=w.id WHERE w.id=$1::uuid",
                        work_id,
                    )
                ],
            )
            work = await connection.fetchrow(
                "SELECT status,last_error_code FROM qc_inspection_work_item WHERE id=$1::uuid",
                work_id,
            )
            assert dict(work) == {
                "status": "failed",
                "last_error_code": "WEB_RULE_CONTRACT_INCOMPATIBLE",
            }, dict(work)
            assert consumed.completed_count == 0
            attempt = await connection.fetchrow(
                "SELECT id::text,status,result_payload FROM qc_inspection_attempt WHERE work_item_id=$1::uuid",
                work_id,
            )
            assert attempt["status"] == "terminal_error"
            original_payload = attempt["result_payload"]
            assert before_write_payloads == [original_payload], (
                "整份原判定必须在真实批量写回前后完全不变"
            )
            decisions = json.loads(original_payload)["decisions"]
            assert [item["execution_status"] for item in decisions] == ["pass", "pass"]
            members = await connection.fetch(
                "SELECT rule_id::text,status,rule_execution_id,observation_packet_id::text,last_error_code FROM qc_inspection_work_item_rule WHERE work_item_id=$1::uuid ORDER BY ordinal",
                work_id,
            )
            first_written = conflict_index == 1 and independent_first not in {
                "missing",
                "forged",
                "different",
            }
            assert [item["status"] for item in members] == (
                ["written", "error"] if first_written else ["error", "error"]
            )
            if independent_first in {"missing", "forged", "different"}:
                assert (
                    dict(
                        await connection.fetchrow(
                            "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                            ids["task"],
                            rules[0],
                        )
                    )
                    == independent_formal
                )
            for member in members[(1 if first_written else 0) :]:
                assert (
                    member["rule_execution_id"] is None
                    and member["last_error_code"] == "WEB_RULE_CONTRACT_INCOMPATIBLE"
                )
            assert (
                dict(
                    await connection.fetchrow(
                        "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                        ids["task"],
                        rules[conflict_index],
                    )
                )
                == prior
            )

            from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
            from services.api.app.infrastructure.db.web_sequence_result_projection import (
                _scopes,
                _source,
            )

            scopes = await _scopes(connection, work_id)
            for index, member in enumerate(members):
                scope = next(item for item in scopes if item["rule_id"] == member["rule_id"])
                decision = next(item for item in decisions if item["rule_id"] == member["rule_id"])
                if index == 0 and first_written:
                    verified = await _source(
                        connection,
                        scope,
                        attempt["id"],
                        execution_status="pass",
                        packet_id=decision["observation_packet_id"],
                    )
                    assert (
                        verified["work_item_id"] == work_id
                        and verified["attempt_id"] == attempt["id"]
                    )
                    formal = await connection.fetchrow(
                        "SELECT judge_output FROM qc_rule_execution WHERE id=$1",
                        member["rule_execution_id"],
                    )
                    assert (
                        verified
                        in json.loads(formal["judge_output"])["web_composite_provenance"]["sources"]
                    )
                    assert len(
                        json.loads(formal["judge_output"])["web_composite_provenance"]["sources"]
                    ) == (1 if stable_current and not independent_first else 2)
                    assert await connection.fetchval(
                        "SELECT execution_status FROM qc_rule_execution WHERE id=$1",
                        member["rule_execution_id"],
                    ) == ("pass" if stable_current and not independent_first else "fail")
                    for forged_scope, status in (
                        (dict(scope, contract_hash="forged-contract"), "pass"),
                        (scope, "error"),
                    ):
                        with pytest.raises(RuleExecutionLeaseConflictError):
                            await _source(
                                connection,
                                forged_scope,
                                attempt["id"],
                                execution_status=status,
                                packet_id=decision["observation_packet_id"],
                            )
                else:
                    with pytest.raises(RuleExecutionLeaseConflictError):
                        await _source(
                            connection,
                            scope,
                            attempt["id"],
                            execution_status="pass",
                            packet_id=decision["observation_packet_id"],
                        )
            cold, cold_packets = await consume_sequence_with_actual_work(
                connection, pool, store, ids, claim.lease.consumer_agent_run_id, monkeypatch
            )
            assert cold.claimed_count == 0 and not cold_packets
            await PostgresInspectionWorkItemRepository(pool=pool).finalize_task_work(
                task_id=ids["task"], error_code="FINAL_GATE", error_message="最终收口"
            )
            assert (
                await connection.fetchval(
                    "SELECT result_payload FROM qc_inspection_attempt WHERE id=$1::uuid",
                    attempt["id"],
                )
                == original_payload
            )
            assert (
                await connection.fetchval(
                    "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", work_id
                )
                == "failed"
            )
            if independent_first:
                assert (
                    await connection.fetchval(
                        "SELECT result_payload FROM qc_inspection_attempt WHERE id=$1",
                        independent_attempt["id"],
                    )
                    == independent_attempt["result_payload"]
                )
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication_work_item WHERE publication_id IN (SELECT id FROM qc_h5_inspection_publication WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_inspection_work_item_rule WHERE work_item_id IN (SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
            await connection.execute("DELETE FROM qc_rule WHERE id=ANY($1::uuid[])", rules)


@pytest.mark.asyncio
async def test_stable_batch_keeps_written_rule_without_its_own_sequence(monkeypatch):
    await test_real_batch_contract_conflict_preserves_written_sources_and_closes_work(
        1, monkeypatch, stable_current=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["valid", "missing", "forged", "different"])
async def test_batch_scope_does_not_authorize_unrelated_work_and_replays_only_bound_legacy_source(
    history, monkeypatch
):
    await test_real_batch_contract_conflict_preserves_written_sources_and_closes_work(
        1, monkeypatch, stable_current=True, independent_first=history
    )
