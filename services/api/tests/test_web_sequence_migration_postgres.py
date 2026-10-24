"""Web 序列持久授权必须由真实平台、Run、会话和原始采集共同满足。"""

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _seed_completed_action_evidence,
)
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离PG双DSN"
)


async def _seed_sequence_anchor(connection, ids, *, command_sequence=1):
    await connection.execute(
        'UPDATE qc_task SET task_config=task_config||\'{"execution_mode":"full_inspection",'
        '"web_inspection_notification_mode":"enforced"}\'::jsonb WHERE id=$1::uuid',
        ids["task"],
    )
    before, after = await _seed_completed_action_evidence(
        connection,
        ids,
        action_execution_id="t9-" + str(uuid4()),
        intent_id="t9-" + str(uuid4()),
        command_sequence=command_sequence,
        requested_source_capture_id=ids["capture"],
        parent_capture_id=ids["capture"],
    )
    await connection.execute(
        "UPDATE qc_capture SET metadata='{" + '"finalization_epoch":0' + "}'::jsonb "
        "WHERE id=ANY($1::uuid[])",
        [before, after],
    )
    await connection.execute(
        "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
        after,
        ids["session"],
    )
    return after


async def _insert_sequence(connection, ids, anchor):
    sequence_id = str(uuid4())
    await connection.execute(
        "INSERT INTO qc_web_evidence_sequence(sequence_id,task_id,source_agent_run_id,"
        "session_id,finalization_epoch,anchor_capture_id,target_id,occurrence_id,purpose,"
        "readiness_status,sequence_hash,sequence_payload) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,$4,0,$5::uuid,$6,$7,'failure_sequence',"
        "'timeout',$8,$9::jsonb)",
        sequence_id,
        ids["task"],
        ids["run"],
        ids["session"],
        anchor,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        json.dumps({"frames": []}),
    )
    return sequence_id


@pytest.mark.asyncio
async def test_web_sequence_requires_real_web_scope_and_is_immutable():
    async with target_database() as (connection, _pool, _other, ids):
        anchor = await _seed_sequence_anchor(connection, ids)
        sequence_id = await _insert_sequence(connection, ids, anchor)
        with pytest.raises(asyncpg.CheckViolationError, match="WEB_SEQUENCE_IMMUTABLE"):
            await connection.execute(
                "UPDATE qc_web_evidence_sequence SET sequence_hash=$2 WHERE sequence_id=$1::uuid",
                sequence_id,
                "d" * 64,
            )
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"]
        )
        with pytest.raises(asyncpg.CheckViolationError, match="WEB_SEQUENCE_SCOPE"):
            await _insert_sequence(connection, ids, anchor)


@pytest.mark.asyncio
async def test_empty_sequence_cannot_authorize_null_assessment_publication():
    async with target_database() as (connection, _pool, _other, ids):
        anchor = await _seed_sequence_anchor(connection, ids)
        sequence_id = await _insert_sequence(connection, ids, anchor)
        await connection.execute(
            "INSERT INTO qc_page_capture(id,task_id,page_seq,page_slug,url,capture_status) "
            "VALUES($1::uuid,$2::uuid,1,'sequence','https://example.test/','captured')",
            anchor,
            ids["task"],
        )
        with pytest.raises(asyncpg.CheckViolationError, match="WEB_SEQUENCE_PUBLICATION"):
            await connection.execute(
                "INSERT INTO qc_h5_inspection_publication(id,task_id,source_agent_run_id,"
                "page_capture_id,ruleset_id,ruleset_version,ruleset_snapshot_hash,finalization_epoch,"
                "input_hash,mode,web_evidence_sequence_id) "
                "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,1,'web-budget-integration',"
                "0,$6,'enforced',$7::uuid)",
                str(uuid4()),
                ids["task"],
                ids["run"],
                anchor,
                ids["ruleset"],
                "e" * 64,
                sequence_id,
            )


@pytest.mark.asyncio
async def test_unbound_sequence_reader_cannot_invent_work_authority():
    from services.api.app.application.observation_packet import ObservationPacketValidationError
    from services.api.app.infrastructure.db.web_sequence_binding_reader import (
        PostgresWebSequenceBindingReader,
    )

    async with target_database() as (connection, pool, _other, ids):
        anchor = await _seed_sequence_anchor(connection, ids)
        await _insert_sequence(connection, ids, anchor)
        with pytest.raises(ObservationPacketValidationError, match="Web"):
            await PostgresWebSequenceBindingReader(pool=pool).load_authorized(
                task_id=ids["task"],
                page_capture_id=anchor,
                source_agent_run_id=ids["run"],
                input_set_id="evidence-input-set:" + "f" * 64,
                rule_id=str(uuid4()),
                rule_contract_hash="f" * 64,
            )


@pytest.mark.asyncio
async def test_published_sequence_rejects_new_member_outside_immutable_payload():
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    from services.api.tests.web_sequence_pg_support import seed_failure_frames

    async with target_database() as (connection, pool, _other, ids):
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            result = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=ids["capture"],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="timeout",
            )
            extra = await _seed_sequence_anchor(connection, ids, command_sequence=3)
            captured_at = await connection.fetchval(
                "SELECT captured_at FROM qc_capture WHERE id=$1::uuid", extra
            )
            with pytest.raises(asyncpg.CheckViolationError, match="WEB_SEQUENCE_MEMBER"):
                await connection.execute(
                    "INSERT INTO qc_web_evidence_sequence_member(sequence_id,ordinal,task_id,capture_id,captured_at,frame_payload) "
                    "VALUES($1::uuid,5,$2::uuid,$3::uuid,$4,$5::jsonb)",
                    result["sequence_id"],
                    ids["task"],
                    extra,
                    captured_at,
                    json.dumps({"capture_id": extra, "capture_role": "after_action"}),
                )
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )


@pytest.mark.asyncio
async def test_failure_registration_does_not_fabricate_second_target_sample():
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )

    async with target_database() as (connection, pool, _other, ids):
        anchor = await _seed_sequence_anchor(connection, ids)
        result = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=ids["capture"],
            evidence_capture_ids=(anchor,),
            page_role="menu",
            readiness_status="timeout",
        )
        assert result["status"] == "evidence_insufficient"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_web_evidence_sequence WHERE task_id=$1::uuid", ids["task"]
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_claim", [False, True])
async def test_real_cancel_prevents_new_sequence_claim_or_seal(cancel_before_claim):
    from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
        PostgresH5InspectionPublicationRepository,
    )
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
    from services.api.tests.web_sequence_pg_support import seed_failure_frames

    async with target_database() as (connection, pool, _other, ids):
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            registered = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=ids["capture"],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="loading",
            )
            publications = PostgresH5InspectionPublicationRepository(pool=pool)
            cancel = PostgresAgentRunStateStore(pool=pool)
            if cancel_before_claim:
                assert await cancel.record_cancel_requested(ids["run"])
            claim = await publications.claim(
                publication_id=registered["publication_id"],
                task_id=ids["task"],
                message_version=1,
                lease_owner="t9-cancel",
                lease_seconds=60,
            )
            if cancel_before_claim:
                assert claim.disposition.value == "stale"
            else:
                assert await cancel.record_cancel_requested(ids["run"])
                with pytest.raises(RuntimeError, match="REJECTED"):
                    await publications.process_web_sequence(lease=claim.lease)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_evidence_input_set WHERE task_id=$1::uuid", ids["task"]
                )
                == 0
            )
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("starts_at_observed_after", [False, True])
async def test_real_failure_registration_reuses_existing_publication_outbox_and_is_idempotent(
    starts_at_observed_after,
):
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    from services.api.tests.web_sequence_pg_support import seed_failure_frames

    async with target_database() as (connection, pool, _other, ids):
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            repository = PostgresWebEvidenceSequenceRepository(pool=pool)
            kwargs = dict(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=captures[1] if starts_at_observed_after else ids["capture"],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="timeout",
            )
            first = await repository.register_failure(**kwargs)
            assert first["status"] == "published", first
            second = await repository.register_failure(**kwargs)
            assert first["publication_id"] == second["publication_id"]
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid AND aggregate_type='h5_inspection_publication'",
                    ids["task"],
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_web_logical_target WHERE task_id=$1::uuid", ids["task"]
                )
                == 0
            )
            from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
                PostgresH5InspectionPublicationRepository,
            )

            claim = await PostgresH5InspectionPublicationRepository(pool=pool).claim(
                publication_id=first["publication_id"],
                task_id=ids["task"],
                message_version=1,
                lease_owner="t9-worker",
                lease_seconds=60,
            )
            assert claim.disposition.value == "claimed"
            assert claim.lease.web_evidence_sequence_id == first["sequence_id"]
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "missing_id",
        "cross_run",
        "false_source",
        "false_terminal",
        "false_blocked",
        "before_document_skew",
        "after_document_skew",
        "cancel_requested",
    ],
)
async def test_failure_registration_rejects_incomplete_or_forged_source(corruption):
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    from services.api.tests.web_sequence_pg_support import seed_failure_frames

    async with target_database() as (connection, pool, _other, ids):
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            kwargs = dict(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=ids["capture"],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="loading",
            )
            if corruption == "missing_id":
                kwargs["evidence_capture_ids"] += (str(uuid4()),)
            elif corruption == "cross_run":
                async with target_database() as (_connection, _pool, _other_pool, foreign):
                    kwargs["evidence_capture_ids"] += (foreign["capture"],)
                    result = await PostgresWebEvidenceSequenceRepository(
                        pool=pool
                    ).register_failure(**kwargs)
                    assert result["status"] == "evidence_insufficient", result
                    return
            elif corruption == "false_source":
                kwargs["source_capture_id"] = captures[3]
            elif corruption == "false_terminal":
                kwargs["readiness_status"] = "stable"
            elif corruption == "cancel_requested":
                from services.api.app.infrastructure.queue.state_store import (
                    PostgresAgentRunStateStore,
                )

                assert await PostgresAgentRunStateStore(pool=pool).record_cancel_requested(
                    ids["run"]
                )
            elif corruption in {"before_document_skew", "after_document_skew"}:
                await connection.execute(
                    "UPDATE qc_capture SET metadata=jsonb_set(metadata,"
                    "'{pipeline_metrics,web_readiness,capture_consistency,after,document_time_origin_ms}',"
                    "'99999'::jsonb) WHERE id=$1::uuid",
                    captures[0] if corruption == "before_document_skew" else captures[3],
                )
            else:
                kwargs["readiness_status"] = "human_verification_blocked"
            result = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
                **kwargs
            )
            assert result["status"] == "evidence_insufficient", result
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_web_evidence_sequence WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == 0
            )
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("session_closed", [False, True])
@pytest.mark.parametrize("after_seal_corruption", [False, True])
async def test_real_publication_seals_each_rule_and_reuses_normal_work_queue(
    monkeypatch, session_closed, after_seal_corruption
):
    from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
        PostgresH5InspectionPublicationRepository,
    )
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    from services.api.tests.test_web_rule_evidence_contract import _contract
    from services.api.tests.web_sequence_pg_support import seed_failure_frames

    async with target_database() as (connection, pool, _other, ids):
        rule_ids = [str(uuid4()), str(uuid4())]
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            for index, rule_id in enumerate(rule_ids):
                contract = _contract()
                contract["minimum_duration_ms"] = index * 1000
                await connection.execute(
                    "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,"
                    "severity,status,check_point,input_contract,judge_config) VALUES($1::uuid,$2::uuid,$3,'T9序列',"
                    "'interaction','llm','medium','enabled','检查持续加载',$4::jsonb,$5::jsonb)",
                    rule_id,
                    ids["ruleset"],
                    f"t9_sequence_{index}",
                    json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
                    json.dumps({"tool_refs": ["semantic_rule_judge_tool"]}),
                )
                await connection.execute(
                    "UPDATE qc_rule SET manual_review_policy='{\"min_confidence\":0.8}'::jsonb WHERE id=$1::uuid",
                    rule_id,
                )
            result = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=ids["capture"],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="timeout",
            )
            publications = PostgresH5InspectionPublicationRepository(pool=pool)
            claim = await publications.claim(
                publication_id=result["publication_id"],
                task_id=ids["task"],
                message_version=1,
                lease_owner="t9-worker",
                lease_seconds=60,
            )
            assert claim.lease.page_state == "loading"
            from dataclasses import replace

            from services.api.app.agents.runtime.h5_inspection_publication_wakeup import (
                H5InspectionPublicationWakeupHandler,
            )
            from services.api.tests.test_h5_inspection_publication_delivery import _wakeup

            await connection.execute(
                "UPDATE qc_h5_inspection_publication SET lease_expires_at=now()-interval '1 second' WHERE id=$1::uuid",
                result["publication_id"],
            )

            class StablePublisherMustNotRun:
                async def publish_for_capture(self, **kwargs):
                    raise AssertionError("未分类序列不能走普通稳定页发布")

            handler = H5InspectionPublicationWakeupHandler(
                repository=publications,
                publisher=StablePublisherMustNotRun(),
                lease_owner="t9-worker",
                lease_seconds=60,
            )
            assert await handler.handle(
                replace(_wakeup(), task_id=ids["task"], aggregate_id=result["publication_id"])
            )
            members = await connection.fetch(
                "SELECT member.evidence_input_set_id, member.contract_hash "
                "FROM qc_inspection_work_item_rule member JOIN qc_inspection_work_item work ON work.id=member.work_item_id "
                "WHERE work.task_id=$1::uuid ORDER BY member.rule_code",
                ids["task"],
            )
            assert len(members) == 2
            assert len({row["evidence_input_set_id"] for row in members}) == 2
            assert (
                await connection.fetchval(
                    "SELECT status FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                    result["publication_id"],
                )
                == "published"
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid AND aggregate_type='inspection_work_item'",
                    ids["task"],
                )
                >= 1
            )
            from services.api.tests.web_sequence_consumer_pg_support import (
                consume_sequence_with_actual_work,
            )

            if after_seal_corruption:
                await connection.execute(
                    "UPDATE qc_capture SET metadata=jsonb_set(metadata,"
                    "'{pipeline_metrics,web_readiness,capture_consistency,after,document_time_origin_ms}',"
                    "'99999'::jsonb) WHERE id=$1::uuid",
                    captures[3],
                )
            if session_closed:
                from services.api.app.infrastructure.db.surface_session_repository import (
                    PostgresSurfaceSessionRepository,
                )

                sessions = PostgresSurfaceSessionRepository(pool=pool)
                draining = await sessions.begin_draining(
                    task_id=ids["task"], session_id=ids["session"], expected_row_version=0
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
            consumed, packets = await consume_sequence_with_actual_work(
                connection, pool, _store, ids, claim.lease.consumer_agent_run_id, monkeypatch
            )
            assert len(packets) == (0 if after_seal_corruption else 2), consumed
            if not after_seal_corruption:
                assert consumed.completed_count == 1
                executions = await connection.fetch(
                    "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid",
                    ids["task"],
                )
                assert len(executions) == 2 and all(
                    row["execution_status"] == "pass" for row in executions
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
