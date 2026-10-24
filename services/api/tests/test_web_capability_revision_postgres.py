"""实际默认通知的局部能力修订必须落盘并改变显式规则计划。"""

import asyncio
import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_rule_evidence_contract import _contract
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_sequence_runtime_pg_support import (
    prepare_sequence_with_default_notification,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离PG双DSN"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "update", "delete", "layer", "final_na", "final_mixed", "unknown"])
async def test_default_notification_persists_local_revision_and_refreshes_only_explicit_rule_plan(
    monkeypatch, mutation
):
    async with target_database() as (connection, pool, _other, ids):
        rule_ids = [str(uuid4())] if mutation == "final_na" else [str(uuid4()), str(uuid4())]
        try:
            await connection.execute(
                "UPDATE qc_task SET task_config=task_config||$2::jsonb WHERE id=$1::uuid",
                ids["task"],
                json.dumps(
                    {"web_exploration_target": {"required_page_types": ["content_list", "content_detail"]}}
                ),
            )
            for index, rule_id in enumerate(rule_ids):
                contract = _contract()
                contract.update(
                    accepted_roles=["failure_sequence", "interaction_sequence"],
                    minimum_duration_ms=0,
                )
                if index == 0:
                    contract["local_structure"] = {
                        "schema_version": "web-local-structure-rule.v1",
                        "scope": "current_document_light_dom",
                        "predicate": "native_video_element_present",
                        "reuse_policy": "same_material_context",
                    }
                await connection.execute(
                    "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,status,"
                    "check_point,input_contract,judge_config,manual_review_policy) VALUES($1::uuid,$2::uuid,$3,'局部结构回归',"
                    "'interaction','llm','medium','enabled','按真实材料判断',$4::jsonb,$5::jsonb,'{\"min_confidence\":0.8}')",
                    rule_id,
                    ids["ruleset"],
                    f"t9_local_{index}",
                    json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
                    json.dumps({"tool_refs": ["semantic_rule_judge_tool"]}),
                )
            required_before = await connection.fetchval(
                "SELECT task_config->'web_exploration_target'->'required_page_types' "
                "FROM qc_task WHERE id=$1::uuid",
                ids["task"],
            )
            revision_ids = []
            retained_store = {}
            for count in (0, 1):
                with monkeypatch.context() as local_patch:
                    result, published, _store, _source, _notification = await asyncio.wait_for(
                        prepare_sequence_with_default_notification(
                            connection,
                            pool,
                            ids,
                            local_patch,
                            interaction=True,
                            native_video_count=None if mutation == "unknown" else count,
                        ),
                        timeout=8,
                    )
                # 同一任务对象存储跨轮保留原始字节，不为后续序列丢掉原前帧材料。
                assert all(
                    path not in retained_store or retained_store[path] == content
                    for path, content in _store.items()
                )
                retained_store.update(_store)
                _store = retained_store
                revision = await connection.fetchrow(
                    "SELECT revision_id::text,revision,assessment_payload FROM qc_web_capability_revision "
                    "WHERE task_id=$1::uuid AND capture_id=$2::uuid",
                    ids["task"],
                    result.ready_capture_id,
                )
                if mutation == "unknown":
                    assert revision is None
                    repository = PostgresH5InspectionPublicationRepository(pool=pool)
                    claim = await repository.claim(
                        publication_id=published[0]["publication_id"], task_id=ids["task"],
                        message_version=1, lease_owner="final-local-unknown", lease_seconds=60,
                    )
                    assert len(await repository.process_web_sequence(lease=claim.lease)) == 1
                    selected = json.loads(await connection.fetchval(
                        "SELECT execution_plan FROM qc_h5_inspection_publication_rule "
                        "WHERE publication_id=$1::uuid AND rule_id=$2::uuid",
                        published[0]["publication_id"], rule_ids[0],
                    ))["selection"]
                    assert selected["status"] == "evidence_insufficient"
                    assert selected["reason_code"] == "WEB_LOCAL_NATIVE_STRUCTURE_UNKNOWN"
                    assert selected["revision_id"] is None
                    assert await connection.fetchval(
                        "SELECT status FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                        published[0]["publication_id"],
                    ) == "needs_review"
                    assert not await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM qc_evidence_input_set "
                        "WHERE task_id=$1::uuid AND rule_id=$2::uuid)", ids["task"], rule_ids[0],
                    )
                    return
                assert revision is not None, "真实通知必须持久修订，不能只返回纯函数"
                if mutation in ("update", "delete"):
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                (
                                    "UPDATE qc_web_capability_revision SET assessment_payload='{}'::jsonb WHERE revision_id=$1::uuid"
                                    if mutation == "update"
                                    else "DELETE FROM qc_web_capability_revision WHERE revision_id=$1::uuid"
                                ),
                                revision["revision_id"],
                            )
                    return
                proof = json.loads(revision["assessment_payload"])
                assert proof["status"] == ("local_absent" if count == 0 else "present")
                assert proof["product_capability_status"] == "unknown"
                metadata = json.loads(
                    await connection.fetchval(
                        "SELECT metadata FROM qc_capture WHERE id=$1::uuid", result.ready_capture_id
                    )
                )
                assert proof["visible_layer"] == {
                    key: metadata["presentation"][key]
                    for key in ("topmost_layer_kind", "base_page_visibility", "blocking_scope")
                }
                revision_ids.append(revision["revision_id"])
                publications = PostgresH5InspectionPublicationRepository(pool=pool)
                claim = await publications.claim(
                    publication_id=published[0]["publication_id"],
                    task_id=ids["task"],
                    message_version=1,
                    lease_owner="t9-local",
                    lease_seconds=60,
                )
                work_ids = await publications.process_web_sequence(lease=claim.lease)
                assert bool(work_ids) is (mutation != "final_na")
                rows = await connection.fetch(
                    "SELECT rule_id::text,execution_plan FROM qc_h5_inspection_publication_rule "
                    "WHERE publication_id=$1::uuid ORDER BY rule_code",
                    published[0]["publication_id"],
                )
                assert json.loads(rows[0]["execution_plan"])["selection"]["status"] == (
                    "not_applicable" if count == 0 else "admissible"
                )
                if len(rows) > 1:
                    assert json.loads(rows[1]["execution_plan"])["selection"]["status"] == "admissible"
                assert (
                    json.loads(rows[0]["execution_plan"])["selection"]["revision_id"]
                    == revision["revision_id"]
                )
                from services.api.tests.web_sequence_consumer_pg_support import (
                    consume_sequence_with_actual_work,
                )

                if mutation in {"final_na", "final_mixed"}:
                    if work_ids:
                        consumed, packets = await consume_sequence_with_actual_work(
                            connection, pool, _store, ids,
                            claim.lease.consumer_agent_run_id, monkeypatch,
                        )
                        assert consumed.completed_count == 1 and len(packets) == 1
                    assert await connection.fetchval(
                        "SELECT count(*) FROM qc_evidence_input_set WHERE task_id=$1::uuid",
                        ids["task"],
                    ) == (1 if work_ids else 0)
                    from services.api.app.infrastructure.db.task_finalization_repository import (
                        _GATE_FACTS_SQL,
                    )

                    await connection.execute(
                        "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
                        ids["task"], ids["run"],
                    )
                    await connection.execute(
                        "UPDATE qc_agent_run SET status='done',stop_reason='completed',finished_at=now() "
                        "WHERE id=$1::uuid", ids["run"],
                    )
                    gate_sql = _GATE_FACTS_SQL.replace(
                        "AS nonterminal_enforced_work_item_count,",
                        "AS nonterminal_enforced_work_item_count, row_to_json(h5_publication_gate) AS sequence_gate,",
                    )
                    gate = await connection.fetchrow(gate_sql, ids["task"], "completed_with_gaps")
                    assert json.loads(gate["sequence_gate"]) == {
                        "active_or_inconsistent": 0, "review_required": 0, "execution_inflight": 0,
                    }
                    from services.api.tests.web_local_absent_gate_pg_support import (
                        assert_local_selection_fences,
                        assert_real_local_finalization,
                    )

                    await assert_local_selection_fences(
                        connection, ids=ids, publication_id=published[0]["publication_id"],
                        rule_id=rule_ids[0], gate_sql=gate_sql,
                    )
                    await assert_real_local_finalization(
                        connection, pool, ids=ids,
                        consumer_run_id=claim.lease.consumer_agent_run_id,
                    )
                    return

                if mutation == "layer":
                    await connection.execute(
                        "UPDATE qc_capture SET metadata=jsonb_set(metadata,'{presentation,topmost_layer_kind}',"
                        "'\"overlay\"'::jsonb) WHERE id=$1::uuid",
                        result.ready_capture_id,
                    )

                with monkeypatch.context() as consumer_patch:
                    consumed, packets = await consume_sequence_with_actual_work(
                        connection,
                        pool,
                        _store,
                        ids,
                        claim.lease.consumer_agent_run_id,
                        consumer_patch,
                    )
                if mutation == "layer":
                    assert not packets
                    assert not await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM qc_rule_execution WHERE task_id=$1::uuid "
                        "AND execution_status='pass')",
                        ids["task"],
                    )
                    return
                assert consumed.completed_count == 1, await connection.fetch(
                    "SELECT attempt.status,attempt.error_code,attempt.error_message "
                    "FROM qc_inspection_attempt attempt "
                    "JOIN qc_inspection_work_item work ON work.id=attempt.work_item_id "
                    "WHERE work.task_id=$1::uuid", ids["task"],
                )
                assert len(packets) == count + 1
                if count:
                    local_packet = next(
                        packet for packet in packets if packet.rule_id == rule_ids[0]
                    )
                    assert (
                        local_packet.packet_payload["facts"]["web_sequence"]["capability_revision"][
                            "revision_id"
                        ]
                        == revision["revision_id"]
                    )
                formal = await connection.fetch(
                    "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid "
                    "AND page_capture_id=$2::uuid",
                    ids["task"],
                    result.ready_capture_id,
                )
                assert len(formal) == count + 1 and all(
                    row["execution_status"] == "pass" for row in formal
                )
            assert len(set(revision_ids)) == 2
            assert json.loads(required_before) == ["content_list", "content_detail"]
            assert (
                await connection.fetchval(
                    "SELECT max(revision) FROM qc_web_capability_revision WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == 2
            )
            assert (
                await connection.fetchval(
                    "SELECT task_config->'web_exploration_target'->'required_page_types' "
                    "FROM qc_task WHERE id=$1::uuid",
                    ids["task"],
                )
                == required_before
            )
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication_work_item WHERE publication_id IN "
                "(SELECT id FROM qc_h5_inspection_publication WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_inspection_work_item_rule WHERE work_item_id IN "
                "(SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
            await connection.execute("DELETE FROM qc_rule WHERE id=ANY($1::uuid[])", rule_ids)
