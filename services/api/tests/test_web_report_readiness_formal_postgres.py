"""真实失败登记、原队列消费和正式冻结；采样、传输及模型为明确替身。"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from services.api.tests import test_web_v2_formal_report_e2e as formal
from services.api.tests.test_web_rule_evidence_contract import _contract
from services.api.tests.web_sequence_consumer_pg_support import consume_sequence_with_actual_work
from services.api.tests.web_sequence_pg_support import seed_failure_frames


@pytest.fixture
async def committed_postgres_case():
    fixture = formal.committed_postgres_case.__wrapped__()
    case = await anext(fixture)
    try:
        yield case
    finally:
        from services.api.tests.web_sequence_closeout_audit_support import (
            export_sequence_closeout_case,
        )

        try:
            await export_sequence_closeout_case(case)
        finally:
            # 序列 WorkRule 外键有额外围栏：按本随机租户清理测试数据，不涉及历史样本。
            async with case.connection.transaction():
                await case.connection.execute("SET LOCAL session_replication_role=replica")
                await case.connection.execute(
                    "DELETE FROM qc_inspection_work_item_rule WHERE work_item_id IN "
                    "(SELECT work.id FROM qc_inspection_work_item work JOIN qc_task task ON "
                    "task.id=work.task_id WHERE task.tenant_id=$1::uuid)",
                    case.tenant_id,
                )
            await fixture.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", ["timeout", "loading"])
async def test_real_failure_exit_survives_worker_formal_binding(
    committed_postgres_case, monkeypatch, exit_reason, selection_mode="selected"
):
    from services.api.app.application.report_insight import ReportInsightService
    from services.api.app.application.web_run_policy import load_published_web_policy
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )

    case = committed_postgres_case
    connection, pool = case.connection, case.pool
    # 新用例只启用明确允许未分类 failure_sequence 的规则，不修改既有恢复用例配置。
    await connection.execute(
        "UPDATE qc_rule SET status='disabled' WHERE ruleset_id=$1::uuid", case.ruleset_id
    )
    contract = _contract()
    if selection_mode == "not_applicable":
        contract["accepted_roles"] = ["interaction_sequence"]
        contract["accept_unclassified"] = False
    input_contract = {"target_clients": ["WEB"], "web_evidence": contract}
    if selection_mode == "zero_candidates":
        input_contract.pop("web_evidence")
    await connection.execute(
        "UPDATE qc_rule SET status='enabled',executor_type='llm',category='interaction',"
        "input_contract=$2::jsonb,judge_config=$3::jsonb,applicable_page_types='[]'::jsonb "
        "WHERE id=$1::uuid",
        case.observed_rule_id,
        json.dumps(input_contract),
        json.dumps({"tool_refs": ["semantic_rule_judge_tool"]}),
    )
    response = await formal._post_task(case)
    assert response.status_code == 202, response.text
    task_id, run_id = response.json()["task_id"], response.json()["run_id"]
    session_id = "web:" + run_id
    ids = {
        "task": task_id,
        "run": run_id,
        "session": session_id,
        "ruleset": case.ruleset_id,
        "capture": str(uuid4()),
    }
    await connection.execute(
        "UPDATE qc_task SET status='running',finalization_state='open',"
        "finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
        task_id,
        run_id,
    )
    await connection.execute(
        "UPDATE qc_agent_run SET status='running',started_at=now() WHERE id=$1::uuid", run_id
    )
    await connection.execute(
        "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,"
        "status,activated_at) "
        "VALUES($1,$2::uuid,'web','web-v2-workflow','active',now())",
        session_id,
        task_id,
    )
    await PostgresWebRunPolicySnapshotRepository(pool=pool).freeze_or_read(
        WebRunPolicySnapshot.create(
            task_id=task_id,
            agent_run_id=run_id,
            payload=load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=300),
        )
    )
    await connection.execute(
        "INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,"
        "capture_role,capture_status,metadata) VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,"
        "'probe_before','captured','{\"finalization_epoch\":0}'::jsonb)",
        ids["capture"],
        task_id,
        run_id,
        session_id,
    )
    frames, store = await seed_failure_frames(connection, pool, ids)
    # 初始 probe 也是已承诺 Capture，必须有自己绑定的对象和 Manifest，不能从库存排除。
    from services.api.app.domain.evidence import (
        ArtifactReference,
        EvidenceQualityStatus,
        build_evidence_manifest_reference,
    )
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )

    source_artifacts = []
    for row in await connection.fetch(
        "SELECT artifact_type,storage_path,content_type,checksum,size_bytes "
        "FROM qc_evidence_artifact WHERE page_capture_id=$1::uuid",
        frames[0],
    ):
        artifact_id = str(uuid4())
        path = task_id + "/" + ids["capture"] + "/" + row["artifact_type"]
        store[path] = store[row["storage_path"]]
        assert "sha256:" + sha256(store[path]).hexdigest() == row["checksum"]
        assert len(store[path]) == row["size_bytes"]
        await connection.execute(
            "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
            "storage_path,content_type,checksum,size_bytes) VALUES($1::uuid,$2::uuid,"
            "$3::uuid,$4,'baseline',$5,$6,$7,$8)",
            artifact_id,
            task_id,
            ids["capture"],
            row["artifact_type"],
            path,
            row["content_type"],
            row["checksum"],
            row["size_bytes"],
        )
        source_artifacts.append(
            ArtifactReference(
                artifact_id=artifact_id,
                artifact_type=row["artifact_type"],
                role="probe_before",
                storage_path=path,
                content_type=row["content_type"],
                checksum=row["checksum"],
                size_bytes=row["size_bytes"],
            )
        )
    await PostgresEvidenceManifestRepository(pool=pool).add(
        manifest=build_evidence_manifest_reference(
            task_id=task_id,
            session_id=session_id,
            capture_id=ids["capture"],
            manifest_version=1,
            artifacts=tuple(source_artifacts),
            quality_status=EvidenceQualityStatus.PASS,
            quality_reason="fixture_verified",
            created_at=datetime.now(UTC),
        )
    )
    registered = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        source_capture_id=frames[1],
        evidence_capture_ids=(frames[1], frames[3]),
        page_role="menu",
        readiness_status=exit_reason,
    )
    assert registered.get("sequence_id"), registered
    publications = formal.PostgresH5InspectionPublicationRepository(pool=pool)
    version = await connection.fetchval(
        "SELECT row_version FROM qc_h5_inspection_publication WHERE id=$1::uuid",
        registered["publication_id"],
    )
    claim = await publications.claim(
        publication_id=registered["publication_id"],
        task_id=task_id,
        message_version=version,
        lease_owner="t10-fix1",
        lease_seconds=60,
    )
    assert claim.lease is not None
    work_ids = await publications.process_web_sequence(lease=claim.lease)
    if selection_mode == "selected":
        assert len(work_ids) == 1
        consumed, packets = await consume_sequence_with_actual_work(
            connection, pool, store, ids, claim.lease.consumer_agent_run_id, monkeypatch
        )
        assert consumed.completed_count == 1 and len(packets) == 1
    else:
        assert work_ids == ()
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_input_set WHERE task_id=$1::uuid", task_id
            )
            == 0
        )
        selections = [
            json.loads(row["execution_plan"])["selection"]
            for row in await connection.fetch(
                "SELECT execution_plan FROM qc_h5_inspection_publication_rule WHERE "
                "publication_id=$1::uuid",
                registered["publication_id"],
            )
        ]
        assert selections == (
            [{"status": "not_applicable", "reason_code": "WEB_SEQUENCE_ROLE_REJECTED"}]
            if selection_mode == "not_applicable"
            else []
        )
    assert await connection.fetchval(
        "SELECT status FROM qc_h5_inspection_publication WHERE id=$1::uuid",
        registered["publication_id"],
    ) == ("published" if selection_mode == "selected" else "not_applicable")
    reader = formal.PostgresTaskInsightRepository(pool=pool)
    dynamic = await reader.fetch_web_report_explanation(
        task_id=task_id, agent_run_id=run_id, access_scope=case.access_scope
    )
    expected = dynamic["sequences"][0]
    assert (
        expected["readiness_status"] == "loading"
        and expected["readiness_exit_reason"] == exit_reason
    )
    assert all(frame["page_state"] == "loading" for frame in expected["frames"])
    # 只有终态探索夹具已提供；原真实 Gate 决定能否正式封存，不伪造放行结果。
    await connection.execute(
        "INSERT INTO qc_web_exploration_budget(agent_run_id,site_id,task_id,session_id,"
        "target_origin_sha256,catalog_version,catalog_sha256,state,sealed_at) "
        "VALUES($1::uuid,'t10-fix1',$2::uuid,$3,$4,'t10-fixture',$4,'sealed',now())",
        run_id,
        task_id,
        session_id,
        "a" * 64,
    )
    await connection.execute(
        "UPDATE qc_task SET finalization_state='exploration_draining',"
        "exploration_draining_at=now() WHERE id=$1::uuid",
        task_id,
    )
    await connection.execute(
        "UPDATE qc_agent_run SET status='done',stop_reason='completed',finished_at=now() "
        "WHERE id=$1::uuid",
        run_id,
    )
    # Run终态不能冒充探索工作终态；缺少台账时仍应被原Gate拒绝。
    missing_work = await connection.fetchrow(formal._GATE_FACTS_SQL, task_id, "completed_with_gaps")
    assert missing_work["exploration_terminal"] is False
    await connection.execute(
        "INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,"
        "source_capture_id,protocol_version,payload_hash,work_payload,status,deadline,finished_at) "
        "VALUES($1,'exploration',$2::uuid,$3,'sequence-failure-fixture',$4::uuid,'v1',$5,"
        "'{}'::jsonb,'succeeded',now()+interval '1 minute',now())",
        "sequence-exploration:" + run_id,
        task_id,
        session_id,
        frames[-1],
        "c" * 64,
    )
    work_service = formal.InspectionWorkItemService(
        repository=formal.PostgresInspectionWorkItemRepository(pool=pool)
    )
    capture_publisher = formal.AppCaptureInspectionPublisher(
        task_query=formal.TaskQueryService(repository=formal.PostgresTaskRepository(pool=pool)),
        snapshot_provider=formal._StaticSnapshotProvider(case),
        work_item_service=work_service,
        annotator=formal._NoopAnnotator(),
    )
    from services.api.app.infrastructure.db.web_capture_identity import (
        PostgresWebCaptureIdentityReader,
    )

    active_publisher = formal.H5LiveInspectionPublishService(
        gate_reader=formal.PostgresH5LiveInspectionTaskGateReader(pool=pool),
        capture_reader=formal.PostgresH5EvidenceCaptureReader(pool=pool),
        publisher=capture_publisher,
        web_identity_reader=PostgresWebCaptureIdentityReader(pool=pool),
    )
    snapshot_repo = formal.PostgresWebCoverageSnapshotRepository(pool=pool)
    finalization = formal.TaskFinalizationService(
        formal.PostgresTaskFinalizationRepository(pool=pool)
    )
    reconciliation = formal.WebInspectionReconciliation(
        state_reader=snapshot_repo,
        manifest_finalizer=None,
        publication_publisher=None,
        active_session_drainer=formal.worker_module._WebActiveSessionInspectionDrainer(
            publisher=active_publisher,
            consumer=formal._DeterministicInspectionConsumer(
                case=case, task_id=task_id, run_id=run_id, service=work_service
            ),
        ),
        finalization=finalization,
        snapshot_repository=snapshot_repo,
        gap_rule_reader=snapshot_repo,
        rule_execution_service=formal.RuleExecutionService(
            repository=formal.PostgresTaskRepository(pool=pool)
        ),
        final_handoff=formal.WebPageInspectionNotification(
            reader=formal.PostgresWebCaptureEvidenceRepository(pool=pool),
            inventory=formal.PostgresH5EvidenceInventoryRepository(pool=pool),
            publication_writer=publications,
        ),
    )
    transport = formal._RecordingWakeupTransport()
    relay = formal.OutboxRelay(
        repository=formal._TaskScopedOutboxRepository(pool=pool, task_id=task_id),
        transport=transport,
        lease_owner="t10-fix1-relay",
    )
    await relay.publish_batch(limit=100)
    handler = formal.H5InspectionPublicationWakeupHandler(
        repository=publications,
        publisher=capture_publisher,
        lease_owner="t10-fix1-wakeup",
        lease_seconds=60,
    )
    for lease, receipt in transport.deliveries:
        if lease.envelope.message_type == formal.H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE:
            assert await handler.handle(
                formal.RedisStreamWakeup(
                    stream_name=receipt.stream_name,
                    stream_entry_id=receipt.stream_entry_id,
                    message_id=lease.envelope.message_id,
                    message_type=lease.envelope.message_type,
                    task_id=task_id,
                    aggregate_type=lease.aggregate_type,
                    aggregate_id=lease.aggregate_id,
                    aggregate_version=lease.aggregate_version,
                    idempotency_key=lease.envelope.idempotency_key,
                )
            )
    finished = await formal.PostgresInspectionWorkItemRepository(
        pool=pool
    ).finish_publication_consumer_run_if_idle(
        task_id=task_id, consumer_agent_run_id=claim.lease.consumer_agent_run_id
    )
    if not finished:
        diagnostic = {}
        for name, sql in {
            "run": "SELECT status,input_context FROM qc_agent_run WHERE id=$2::uuid AND "
            "task_id=$1::uuid",
            "outbox": "SELECT status,aggregate_type,message_type FROM qc_outbox_event WHERE "
            "task_id=$1::uuid",
            "inbox": "SELECT status,aggregate_type FROM qc_event_inbox WHERE task_id=$1::uuid",
            "attempts": "SELECT status FROM qc_inspection_attempt WHERE work_item_id IN "
            "(SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
        }.items():
            args = (task_id, claim.lease.consumer_agent_run_id) if name == "run" else (task_id,)
            diagnostic[name] = [dict(row) for row in await connection.fetch(sql, *args)]
        raise AssertionError(json.dumps(diagnostic, ensure_ascii=False))
    assert (
        await connection.fetchval(
            "SELECT status FROM qc_agent_run WHERE id=$1::uuid", claim.lease.consumer_agent_run_id
        )
        == "done"
    )
    from services.api.tests.web_sequence_gate_pg_support import audit_sequence_gate

    await audit_sequence_gate(
        connection,
        task_id=task_id,
        publication_id=registered["publication_id"],
        consumer_run_id=claim.lease.consumer_agent_run_id,
        selected=selection_mode == "selected",
    )

    async def isolated_pool(*_args):
        return pool

    monkeypatch.setattr(formal.worker_module, "get_postgres_pool", isolated_pool)
    monkeypatch.setattr(formal.worker_module, "PostgresTaskInsightRepository", lambda: reader)
    reconciled = await reconciliation.reconcile(
        formal.ReconcileWebInspectionCommand(
            task_id=task_id, agent_run_id=run_id, finalization_epoch=0
        )
    )
    assert reconciled.status == "waiting_for_run_terminal", reconciled
    assert (
        await connection.fetchval(
            "SELECT finalization_state FROM qc_task WHERE id=$1::uuid", task_id
        )
        == "session_draining"
    )
    redis = formal.fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = formal.RedisAgentRunQueue(redis, ttl_seconds=3600, queue_key="t10-fix1:" + task_id)
    try:
        completed = await formal.worker_module._retry_pending_task_finalizations(
            queue,
            formal.PostgresAgentRunStateStore(pool=pool),
            finalization,
            final_report_generator=formal.ReportGenerationService(
                query_service=formal.TaskQueryService(
                    repository=formal.PostgresTaskRepository(pool=pool)
                )
            ),
            web_inspection_reconciliation=reconciliation,
        )
        assert completed == 1, dict(
            await connection.fetchrow(
                "SELECT status,finalization_state,finalization_last_error FROM qc_task "
                "WHERE id=$1::uuid",
                task_id,
            )
        )
        facts = dict(
            await connection.fetchrow(
                "SELECT task.status,task.finalization_state,task.finalization_last_error,"
                "gate.gate_passed,gate.block_reasons,(SELECT count(*) FROM "
                "qc_report_gate_binding WHERE task_id=task.id) AS bindings "
                "FROM qc_task task LEFT JOIN qc_completion_gate_snapshot gate ON "
                "gate.gate_snapshot_id=task.completion_gate_snapshot_id "
                "WHERE task.id=$1::uuid",
                task_id,
            )
        )
        if facts["bindings"] != 1:
            components = (
                "page_enforced_work",
                "aggregate_work",
                "aggregate_expected",
                "aggregate_candidate_gate",
                "qualified_h5_captures",
                "web_classification_gate",
                "web_publication_gate",
                "h5_publication_gate",
            )
            projection = ", ".join(f"row_to_json({name}) AS {name}" for name in components)
            diagnostic_sql = formal._GATE_FACTS_SQL.replace(
                "AS nonterminal_enforced_work_item_count,",
                "AS nonterminal_enforced_work_item_count, " + projection + ",",
            )
            gate_rows = dict(
                await connection.fetchrow(diagnostic_sql, task_id, "completed_with_gaps")
            )
            facts["gate_components"] = {name: gate_rows[name] for name in components}
            facts["runs"] = [
                dict(row)
                for row in await connection.fetch(
                    "SELECT agent_type,status,stop_reason FROM qc_agent_run WHERE task_id=$1::uuid",
                    task_id,
                )
            ]
            facts["works"] = [
                dict(row)
                for row in await connection.fetch(
                    "SELECT status,session_dependency FROM qc_inspection_work_item WHERE "
                    "task_id=$1::uuid",
                    task_id,
                )
            ]
            facts["publications"] = [
                dict(row)
                for row in await connection.fetch(
                    "SELECT status,failure_code FROM qc_h5_inspection_publication WHERE "
                    "task_id=$1::uuid",
                    task_id,
                )
            ]
            raise AssertionError(json.dumps(facts, ensure_ascii=False))
        assert facts["gate_passed"] is True, facts
        service = ReportInsightService(
            repository=reader,
            final_report_snapshot_repository=formal.PostgresTaskRepository(pool=pool),
        )
        frozen = await service.report_insight(task_id, access_scope=case.access_scope)
        assert frozen["summary"]["report_stage"] == "final"
        assert frozen["web_report_explanation"]["sequences"] == [expected]
        _, exported = await service.export_html(task_id, access_scope=case.access_scope)
        from services.api.tests.web_sequence_closeout_audit_support import (
            export_sequence_formal_html,
        )

        export_sequence_formal_html(task_id, exported)
        assert (
            registered["sequence_id"] in exported
            and exit_reason in exported
            and "readiness_exit_reason" in exported
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid", task_id
            )
            == 1
        )
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_mode", ["not_applicable", "zero_candidates"])
async def test_real_zero_batch_sequence_uses_frozen_selection_without_fake_inputs(
    committed_postgres_case, monkeypatch, selection_mode
):
    await test_real_failure_exit_survives_worker_formal_binding(
        committed_postgres_case, monkeypatch, "loading", selection_mode=selection_mode
    )
