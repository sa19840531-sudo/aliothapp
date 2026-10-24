"""readiness 默认通知到原 Publication/Work/Judge 的隔离生产组合。"""

import asyncio
import json
import os
from uuid import UUID, uuid4

import pytest

from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
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

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离PG双DSN"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction", [False, True])
@pytest.mark.parametrize("session_closed", [False, True])
@pytest.mark.parametrize("authority_drift", [None, "cancel_requested", "same_timestamp_successor"])
async def test_actual_readiness_default_notification_reaches_existing_work_and_judge(
    interaction,
    session_closed,
    authority_drift,
    monkeypatch,
    factory_mode=None,
    pending_entry=False,
    read_tamper=None,
):
    async with target_database() as (connection, pool, _other, ids):
        rule_id = str(uuid4())
        try:
            contract = _contract()
            contract.update(
                accepted_roles=["failure_sequence", "interaction_sequence", "stable_page"],
                minimum_duration_ms=0,
            )
            await connection.execute(
                "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,"
                "status,check_point,input_contract,judge_config) VALUES($1::uuid,$2::uuid,'t9_runtime','T9真实出口',"
                "'interaction','llm','medium','enabled','核对实际序列',$3::jsonb,$4::jsonb)",
                rule_id,
                ids["ruleset"],
                json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
                json.dumps({"tool_refs": ["semantic_rule_judge_tool"]}),
            )
            (
                result,
                published,
                store,
                _source,
                _notification,
            ) = await prepare_sequence_with_default_notification(
                connection, pool, ids, monkeypatch, interaction=interaction
            )
            await connection.execute(
                "UPDATE qc_rule SET manual_review_policy='{\"min_confidence\":0.8}'::jsonb WHERE id=$1::uuid",
                rule_id,
            )
            assert result.is_ready is interaction
            assert len(published) == 1
            assert published[0]["status"] == "published", published
            publications = PostgresH5InspectionPublicationRepository(pool=pool)
            if pending_entry:
                from services.api.app.application.inspection_composition import (
                    build_default_web_live_inspection_publish_service,
                )
                from services.api.app.infrastructure.db import (
                    web_sequence_pending_publication as pending,
                )

                repository_type = pending.PostgresWebSequencePendingPublisher
                monkeypatch.setattr(
                    pending,
                    "PostgresWebSequencePendingPublisher",
                    lambda: repository_type(pool=pool),
                )
                batch = (
                    await build_default_web_live_inspection_publish_service().publish_pending_batch(
                        task_id=ids["task"], agent_run_id=ids["run"], limit=1, cursor=None
                    )
                )
                assert batch.attempted_count == batch.created_count == 1
                consumer_run_id = await connection.fetchval(
                    "SELECT consumer_agent_run_id::text FROM qc_h5_inspection_publication "
                    "WHERE id=$1::uuid",
                    published[0]["publication_id"],
                )
            else:
                claim = await publications.claim(
                    publication_id=published[0]["publication_id"],
                    task_id=ids["task"],
                    message_version=1,
                    lease_owner="t9-runtime",
                    lease_seconds=60,
                )
                assert await publications.process_web_sequence(lease=claim.lease)
                consumer_run_id = claim.lease.consumer_agent_run_id
            if session_closed:
                from services.api.app.infrastructure.db.surface_session_repository import (
                    PostgresSurfaceSessionRepository,
                )

                sessions = PostgresSurfaceSessionRepository(pool=pool)
                version = await connection.fetchval(
                    "SELECT row_version FROM qc_surface_session WHERE session_id=$1", ids["session"]
                )
                draining = await sessions.begin_draining(
                    task_id=ids["task"], session_id=ids["session"], expected_row_version=version
                )
                closing = await sessions.seal_for_close(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    expected_row_version=draining.row_version,
                )
                await sessions.mark_closed(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    expected_row_version=closing.row_version,
                )
            if authority_drift == "cancel_requested":
                from services.api.app.infrastructure.queue.state_store import (
                    PostgresAgentRunStateStore,
                )

                assert await PostgresAgentRunStateStore(pool=pool).record_cancel_requested(
                    ids["run"]
                )
                assert (
                    await connection.fetchval(
                        "SELECT status FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
                    )
                    == "running"
                )
            elif authority_drift == "same_timestamp_successor":
                successor = str(UUID(int=UUID(ids["run"]).int + 1))
                await connection.execute(
                    "INSERT INTO qc_agent_run(id,task_id,agent_name,agent_type,status,input_context,created_at) "
                    "SELECT $2::uuid,task_id,'T9后继','main','running',input_context,created_at FROM qc_agent_run WHERE id=$1::uuid",
                    ids["run"],
                    successor,
                )
            consumed, packets = await consume_sequence_with_actual_work(
                connection,
                pool,
                store,
                ids,
                consumer_run_id,
                monkeypatch,
                factory_mode=factory_mode,
                read_tamper=read_tamper,
            )
            if authority_drift or read_tamper:
                assert not packets, "已取消或同时间戳旧Run不得到达业务模型"
                assert not await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM qc_rule_execution WHERE task_id=$1::uuid "
                    "AND execution_status='pass')",
                    ids["task"],
                )
                return
            assert consumed.completed_count == 1
            assert len(packets) == 1
            original = json.loads(
                await connection.fetchval(
                    "SELECT result_payload FROM qc_inspection_attempt WHERE work_item_id IN "
                    "(SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
                    ids["task"],
                )
            )
            decision = original["decisions"][0]
            execution = await connection.fetchrow(
                "SELECT execution_status,observation_packet_id::text FROM qc_rule_execution "
                "WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                ids["task"],
                rule_id,
            )
            assert decision["execution_status"] == execution["execution_status"] == "pass"
            assert decision["observation_packet_id"] == execution["observation_packet_id"]
            facts = packets[0].packet_payload["facts"]["web_sequence"]
            if interaction:
                assert (
                    facts["interaction_outcomes"][0]["action_result_capture_id"]
                    != result.ready_capture_id
                )
                assert (
                    facts["interaction_outcomes"][0]["after_capture_id"] == result.ready_capture_id
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
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rule_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_mode", ["off", "shadow", "on"])
async def test_three_actual_manifest_factories_consume_formal_sequence(factory_mode, monkeypatch):
    await test_actual_readiness_default_notification_reaches_existing_work_and_judge(
        True, False, None, monkeypatch, factory_mode=factory_mode
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read_tamper", ["consumer", "attempt", "work", "token", "expired", "replaced"]
)
async def test_sequence_reader_rejects_another_callers_live_lease(read_tamper, monkeypatch):
    await test_actual_readiness_default_notification_reaches_existing_work_and_judge(
        True, False, None, monkeypatch, read_tamper=read_tamper
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction", [False, True])
async def test_default_live_pending_entry_consumes_existing_sequence_publication(
    interaction, monkeypatch
):
    await test_actual_readiness_default_notification_reaches_existing_work_and_judge(
        interaction, False, None, monkeypatch, pending_entry=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stable_first", [True, False])
@pytest.mark.parametrize("crash_after_write", [False, True])
async def test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
    stable_first,
    crash_after_write,
    monkeypatch,
    publication_order="sequence_first",
    writer_tamper=None,
    contract_drift=False,
    crash_contract_closeout=False,
    terminal_probe=False,
    task_finalize_recovery=False,
):
    if contract_drift:
        import logging

        monkeypatch.setattr(
            logging.getLogger("services.api.app.agents.runtime.inspection_work_item_consumer"),
            "disabled",
            False,
        )
    if crash_contract_closeout:
        from services.api.app.agents.runtime.inspection_work_item_execution import (
            InspectionWorkItemAttempt,
        )

        async def interrupted_closeout(self, **kwargs):
            raise RuntimeError("测试：投影拒绝后、技术收口前中断")

        monkeypatch.setattr(
            InspectionWorkItemAttempt,
            "terminate_web_contract_incompatibility",
            interrupted_closeout,
        )
    if terminal_probe:
        from dataclasses import replace

        from services.api.app.agents.runtime.inspection_work_item_execution import (
            InspectionWorkItemAttempt,
        )
        from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
        from services.api.app.infrastructure.db.inspection_work_item_repository import (
            PostgresInspectionWorkItemRepository,
        )

        original_closeout = InspectionWorkItemAttempt.terminate_web_contract_incompatibility

        async def probe_closeout(self, *, command):
            for field in (
                "work_item_id",
                "attempt_id",
                "agent_run_id",
                "lease_token",
                "observation_packet_id",
            ):
                with pytest.raises(RuleExecutionLeaseConflictError):
                    await self._service._repository.terminate_web_contract_incompatibility(
                        command=replace(command, **{field: str(uuid4())})
                    )
            await original_closeout(self, command=command)

        monkeypatch.setattr(
            InspectionWorkItemAttempt, "terminate_web_contract_incompatibility", probe_closeout
        )
    async with target_database() as (connection, pool, _other, ids):
        rule_id = str(uuid4())
        try:
            contract = _contract()
            contract.update(
                accepted_roles=["failure_sequence", "interaction_sequence", "stable_page"],
                minimum_duration_ms=0,
            )
            await connection.execute(
                "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,"
                "status,check_point,input_contract,judge_config,applicable_page_types) VALUES($1::uuid,$2::uuid,'t9_both','T9两用途',"
                "'interaction','llm','medium','enabled','两种用途独立判定',$3::jsonb,$4::jsonb,'[\"content_detail\"]')",
                rule_id,
                ids["ruleset"],
                json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
                json.dumps({"tool_refs": ["semantic_rule_judge_tool"]}),
            )
            await connection.execute(
                "UPDATE qc_rule SET manual_review_policy='{\"min_confidence\":0.8}'::jsonb WHERE id=$1::uuid",
                rule_id,
            )
            stable_publications = []

            async def publication_hook(publish_sequence, kwargs, source, notification):
                async def stable():
                    stable_publications.append(
                        await publish_stable_anchor(
                            _other,
                            ids,
                            source,
                            notification,
                            kwargs["result"].ready_capture_id,
                            PostgresH5InspectionPublicationRepository(pool=_other),
                            monkeypatch,
                        )
                    )

                if publication_order == "sequence_before_classification":
                    sequence_result = await publish_sequence()
                    await stable()
                    return sequence_result
                if publication_order == "concurrent":
                    sequence_result, _ = await asyncio.wait_for(
                        asyncio.gather(publish_sequence(), stable()), timeout=12
                    )
                    return sequence_result
                await stable()
                return await publish_sequence()

            (
                result,
                published,
                store,
                source,
                notification,
            ) = await prepare_sequence_with_default_notification(
                connection,
                pool,
                ids,
                monkeypatch,
                interaction=True,
                notification_hook=publication_hook
                if publication_order != "sequence_first"
                else None,
            )
            publications = PostgresH5InspectionPublicationRepository(pool=pool)
            version_query = (
                "SELECT row_version,status,attempt_count FROM qc_h5_inspection_publication "
                "WHERE id=$1::uuid"
            )
            sequence_publication_id = published[0]["publication_id"]
            before_claim = await connection.fetchrow(version_query, sequence_publication_id)
            event = await connection.fetchrow(
                "SELECT aggregate_version,envelope_payload FROM qc_outbox_event "
                "WHERE aggregate_type='h5_inspection_publication' "
                "AND aggregate_id=$1 AND aggregate_version=$2",
                sequence_publication_id, before_claim["row_version"],
            )
            assert event is not None, "当前序列版本必须有真实唤醒事件，不能手工拼版本补投"
            envelope = json.loads(event["envelope_payload"])
            assert envelope["aggregate_version"] == event["aggregate_version"]
            assert envelope["payload"]["publication_id"] == sequence_publication_id
            assert envelope["payload"]["message_version"] == event["aggregate_version"]
            if publication_order == "sequence_before_classification":
                assert event["aggregate_version"] > 1, "必须实际覆盖晚到分类推进版本的顺序"
            if event["aggregate_version"] > 1:
                stale = await publications.claim(
                    publication_id=sequence_publication_id, task_id=ids["task"],
                    message_version=1, lease_owner="old-sequence-event", lease_seconds=60,
                )
                assert stale.disposition.value == "stale" and stale.lease is None
                assert await connection.fetchrow(version_query, sequence_publication_id) == before_claim
            seq_claim = await publications.claim(
                publication_id=sequence_publication_id,
                task_id=ids["task"],
                message_version=event["aggregate_version"],
                lease_owner="t9-both",
                lease_seconds=60,
            )
            assert seq_claim.lease is not None, (
                seq_claim,
                await connection.fetchrow(
                    "SELECT status,row_version,web_evidence_sequence_id "
                    "FROM qc_h5_inspection_publication "
                    "WHERE id=$1::uuid", published[0]["publication_id"],
                ),
            )
            if not contract_drift or not stable_first:
                await publications.process_web_sequence(lease=seq_claim.lease)
            stable_notified, stable_lease = (
                stable_publications[0]
                if stable_publications
                else await publish_stable_anchor(
                    pool,
                    ids,
                    source,
                    notification,
                    result.ready_capture_id,
                    publications,
                    monkeypatch,
                    defer_work=contract_drift and not stable_first,
                )
            )
            assert stable_notified.publication_id != published[0]["publication_id"]
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id=$1::uuid "
                    "AND page_capture_id=$2::uuid",
                    ids["task"],
                    result.ready_capture_id,
                )
                == 2
            )
            # 原 Work 优先级只决定测试消费顺序，不修改任何冻结材料或用途。
            original_canonical = None
            for ordinal, is_stable in enumerate([True, False] if stable_first else [False, True]):
                await connection.execute(
                    "UPDATE qc_inspection_work_item w SET priority=CASE WHEN EXISTS("
                    "SELECT 1 FROM qc_inspection_work_item_rule m WHERE m.work_item_id=w.id AND "
                    "m.evidence_input_set_id IS NULL)=$2 THEN 1 ELSE 100 END WHERE w.task_id=$1::uuid",
                    ids["task"],
                    is_stable,
                )
                consumed, packets = await consume_sequence_with_actual_work(
                    connection,
                    pool,
                    store,
                    ids,
                    stable_lease.consumer_agent_run_id,
                    monkeypatch,
                    conclusion="fail" if is_stable and not contract_drift else "pass",
                    crash_after_write=crash_after_write,
                    writer_tamper=writer_tamper if not is_stable else None,
                )
                if contract_drift and ordinal == 1:
                    assert len(packets) == 1, "新合同必须实际判定，不能借旧canonical冷恢复"
                    if crash_contract_closeout:
                        from services.api.app.infrastructure.db.inspection_work_item_repository import (
                            PostgresInspectionWorkItemRepository,
                        )

                        await PostgresInspectionWorkItemRepository(pool=pool).finalize_task_work(
                            task_id=ids["task"],
                            error_code="WEB_RULE_CONTRACT_INCOMPATIBLE",
                            error_message="合同不兼容收口",
                        )
                    work = await connection.fetchrow(
                        "SELECT status,last_error_code AS error_code FROM qc_inspection_work_item WHERE task_id=$1::uuid AND status<>'succeeded'",
                        ids["task"],
                    )
                    assert (
                        work
                        and work["status"] == "failed"
                        and work["error_code"] == "WEB_RULE_CONTRACT_INCOMPATIBLE"
                    ), work
                    current = await connection.fetchrow(
                        "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                        ids["task"],
                        rule_id,
                    )
                    assert dict(current) == original_canonical
                    attempts = await connection.fetch(
                        "SELECT a.status,a.result_payload FROM qc_inspection_attempt a JOIN qc_inspection_work_item w ON w.id=a.work_item_id WHERE w.task_id=$1::uuid ORDER BY a.created_at",
                        ids["task"],
                    )
                    assert [row["status"] for row in attempts] == [
                        "written",
                        "abandoned" if crash_contract_closeout else "terminal_error",
                    ]
                    assert (
                        await connection.fetchval(
                            "SELECT count(*) FROM qc_inspection_work_item_rule m JOIN qc_inspection_work_item w ON w.id=m.work_item_id WHERE w.task_id=$1::uuid AND w.status='failed' AND m.status='error' AND m.rule_execution_id IS NULL",
                            ids["task"],
                        )
                        == 1
                    )
                    assert [
                        json.loads(row["result_payload"])["decisions"][0]["execution_status"]
                        for row in attempts
                    ] == ["pass", "pass"]
                    cold, cold_packets = await consume_sequence_with_actual_work(
                        connection,
                        pool,
                        store,
                        ids,
                        stable_lease.consumer_agent_run_id,
                        monkeypatch,
                    )
                    assert cold.claimed_count == 0 and not cold_packets
                    return
                if writer_tamper and writer_tamper != "no_contract_difference" and not is_stable:
                    assert consumed.completed_count == 0, "伪材料或租约不能被已有fail的保守投影掩盖"
                    original = await connection.fetchrow(
                        "SELECT execution_status,judge_output FROM qc_rule_execution "
                        "WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                        ids["task"],
                        rule_id,
                    )
                    assert original["execution_status"] == "fail"
                    assert (
                        len(
                            json.loads(original["judge_output"])["web_composite_provenance"][
                                "sources"
                            ]
                        )
                        == 1
                    )
                    return
                if crash_after_write:
                    original_rows = await connection.fetch(
                        "SELECT a.id,a.result_payload FROM qc_inspection_attempt a "
                        "JOIN qc_inspection_work_item w ON w.id=a.work_item_id WHERE w.task_id=$1::uuid",
                        ids["task"],
                    )
                    if task_finalize_recovery and ordinal == 1:
                        from services.api.app.infrastructure.db.inspection_work_item_repository import (
                            PostgresInspectionWorkItemRepository,
                        )

                        finalized = await PostgresInspectionWorkItemRepository(
                            pool=pool
                        ).finalize_task_work(
                            task_id=ids["task"],
                            error_code="FINAL_GATE",
                            error_message="正式来源终结恢复",
                        )
                        assert (
                            finalized.recovered_work_item_count == 1
                            and finalized.failed_work_item_count == 0
                        )
                        assert (
                            await connection.fetchval(
                                "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid AND status='succeeded'",
                                ids["task"],
                            )
                            == 2
                        )
                        for original_row in original_rows:
                            assert (
                                await connection.fetchval(
                                    "SELECT result_payload FROM qc_inspection_attempt WHERE id=$1",
                                    original_row["id"],
                                )
                                == original_row["result_payload"]
                            )
                        break
                    await connection.execute(
                        "UPDATE qc_inspection_work_item SET next_attempt_at=now()-interval '1 second' "
                        "WHERE task_id=$1::uuid AND status='retry_wait'",
                        ids["task"],
                    )
                    consumed, recovered_packets = await consume_sequence_with_actual_work(
                        connection,
                        pool,
                        store,
                        ids,
                        stable_lease.consumer_agent_run_id,
                        monkeypatch,
                    )
                    assert not recovered_packets, "已提交本用途应冷恢复而非重跑模型"
                    for original_row in original_rows:
                        assert (
                            await connection.fetchval(
                                "SELECT result_payload FROM qc_inspection_attempt WHERE id=$1",
                                original_row["id"],
                            )
                            == original_row["result_payload"]
                        )
                assert consumed.completed_count == 1, consumed
                current_execution = await connection.fetchrow(
                    "SELECT execution_status,error_code,error_message,judge_output "
                    "FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                    ids["task"],
                    rule_id,
                )
                assert current_execution["execution_status"] != "error", dict(current_execution)
                assert len(packets) == 1, (
                    is_stable,
                    await connection.fetch(
                        "SELECT a.result_payload FROM qc_inspection_attempt a JOIN qc_inspection_work_item w "
                        "ON w.id=a.work_item_id WHERE w.task_id=$1::uuid ORDER BY a.created_at",
                        ids["task"],
                    ),
                )
                if contract_drift and ordinal == 0:
                    original_canonical = dict(
                        await connection.fetchrow(
                            "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                            ids["task"],
                            rule_id,
                        )
                    )
                    contract["rule_version"] = "2"
                    await connection.execute(
                        "UPDATE qc_rule SET input_contract=$2::jsonb WHERE id=$1::uuid",
                        rule_id,
                        json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
                    )
                    if stable_first:
                        assert await publications.process_web_sequence(lease=seq_claim.lease)
                    else:
                        from services.api.tests.web_sequence_stable_pg_support import (
                            complete_stable_publication,
                        )

                        await complete_stable_publication(
                            pool,
                            ids,
                            result.ready_capture_id,
                            publications,
                            stable_lease,
                            monkeypatch,
                        )
            attempts = await connection.fetch(
                "SELECT a.result_payload FROM qc_inspection_attempt a "
                "JOIN qc_inspection_work_item w ON w.id=a.work_item_id WHERE w.task_id=$1::uuid ORDER BY a.created_at",
                ids["task"],
            )
            decisions = [
                json.loads(row["result_payload"])["decisions"][0]
                for row in attempts
                if "decisions" in json.loads(row["result_payload"])
            ]
            assert sorted(item["execution_status"] for item in decisions) == ["fail", "pass"]
            rows = await connection.fetch(
                "SELECT execution_status,judge_output,observation_packet_id::text FROM qc_rule_execution "
                "WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                ids["task"],
                rule_id,
            )
            assert len(rows) == 1
            assert rows[0]["execution_status"] == "fail"
            provenance = json.loads(rows[0]["judge_output"])["web_composite_provenance"]
            assert len(provenance["sources"]) == 2
            failed = next(item for item in decisions if item["execution_status"] == "fail")
            assert rows[0]["observation_packet_id"] == failed["observation_packet_id"]
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
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rule_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publication_order", ["stable_first", "concurrent", "sequence_before_classification"],
)
async def test_same_anchor_publication_order_and_two_connection_race(
    publication_order, monkeypatch
):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        True, False, monkeypatch, publication_order=publication_order
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_tamper", ["evidence", "packet", "lease"])
async def test_canonical_projection_never_hides_new_source_or_lease_corruption(
    writer_tamper, monkeypatch
):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        True, False, monkeypatch, writer_tamper=writer_tamper
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stable_first", [True, False])
async def test_different_frozen_contracts_do_not_merge_or_retry_forever(monkeypatch, stable_first):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        stable_first, False, monkeypatch, contract_drift=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stable_first", [True, False])
async def test_contract_rejection_crash_cannot_borrow_old_canonical_at_task_finalization(
    monkeypatch, stable_first
):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        stable_first, False, monkeypatch, contract_drift=True, crash_contract_closeout=True
    )


@pytest.mark.asyncio
async def test_contract_terminalization_reauthenticates_every_identity(monkeypatch):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        True, False, monkeypatch, contract_drift=True, terminal_probe=True
    )


@pytest.mark.asyncio
async def test_same_contract_cannot_use_technical_terminalization(monkeypatch):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        True, False, monkeypatch, writer_tamper="no_contract_difference"
    )


@pytest.mark.asyncio
async def test_task_finalization_recovers_exact_written_web_source(monkeypatch):
    await test_same_anchor_purposes_keep_both_attempts_and_conservative_canonical(
        True, True, monkeypatch, task_finalize_recovery=True
    )
