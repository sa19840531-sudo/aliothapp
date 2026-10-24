"""局部不适用的最终门禁负向与原事务收口支撑，不改业务门禁或源证据。"""

import json
from copy import deepcopy
from hashlib import sha256


async def assert_local_selection_fences(connection, *, ids, publication_id, rule_id, gate_sql):
    original = json.loads(
        await connection.fetchval(
            "SELECT execution_plan FROM qc_h5_inspection_publication_rule "
            "WHERE publication_id=$1::uuid AND rule_id=$2::uuid",
            publication_id,
            rule_id,
        )
    )
    mutations = []
    for field, value in (
        ("identity_json", "not-json"),
        ("identity_json", {}),
        ("identity_json", "[]"),
        ("declaration", "not-a-declaration"),
        ("declaration", {**original["selection"]["declaration"], "scope": "global"}),
        ("rule_contract_hash", "f" * 64),
        ("rule_version", "forged-version"),
        ("revision_id", "00000000-0000-0000-0000-000000000000"),
        ("reason_code", "UNKNOWN_NOT_APPLICABLE"),
    ):
        changed = deepcopy(original)
        changed["selection"][field] = value
        mutations.append((field + ":" + type(value).__name__, changed))
    legacy = deepcopy(original)
    for field in ("identity_json", "declaration", "rule_version", "rule_contract_hash"):
        legacy["selection"].pop(field)
    mutations.append(("legacy_missing_declaration", legacy))
    forged = deepcopy(original)
    identity = json.loads(forged["selection"]["identity_json"])
    identity["capture_id"] = ids["capture"]
    identity_json = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    forged["selection"].update(
        identity_json=identity_json, reuse_key=sha256(identity_json.encode()).hexdigest()
    )
    mutations.append(("self_consistent_wrong_source", forged))
    for name, plan in mutations:
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "UPDATE qc_h5_inspection_publication_rule SET execution_plan=$3::jsonb "
                "WHERE publication_id=$1::uuid AND rule_id=$2::uuid",
                publication_id,
                rule_id,
                json.dumps(plan),
            )
            gate = await connection.fetchrow(gate_sql, ids["task"], "completed_with_gaps")
            assert json.loads(gate["sequence_gate"])["active_or_inconsistent"] == 1, name
        finally:
            await transaction.rollback()
    print("LOCAL_NA_GATE_REJECTED", [name for name, _ in mutations])
    transaction = connection.transaction()
    await transaction.start()
    try:
        other_run = await connection.fetchval(
            "SELECT id::text FROM qc_agent_run WHERE task_id=$1::uuid AND id<>$2::uuid LIMIT 1",
            ids["task"],
            ids["run"],
        )
        assert other_run is not None
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            other_run,
        )
        gate = await connection.fetchrow(gate_sql, ids["task"], "completed_with_gaps")
        assert json.loads(gate["sequence_gate"])["active_or_inconsistent"] == 1
    finally:
        await transaction.rollback()


async def assert_real_local_finalization(connection, pool, *, ids, consumer_run_id):
    from services.api.app.application.task_finalization import (
        BeginExplorationDrainingCommand,
        BeginSessionDrainingCommand,
        EvaluateCompletionGateCommand,
    )
    from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
    from services.api.app.infrastructure.db.web_payload_readiness_repository import _ConnectionPool
    from services.api.tests import test_web_v2_formal_report_e2e as formal

    publications = formal.PostgresH5InspectionPublicationRepository(pool=pool)
    transport = formal._RecordingWakeupTransport()
    relay = formal.OutboxRelay(
        repository=formal._TaskScopedOutboxRepository(pool=pool, task_id=ids["task"]),
        transport=transport,
        lease_owner="final-local-relay",
    )
    await relay.publish_batch(limit=100)
    handler = formal.H5InspectionPublicationWakeupHandler(
        repository=publications,
        publisher=object(),
        lease_owner="final-local-wakeup",
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
                    task_id=ids["task"],
                    aggregate_type=lease.aggregate_type,
                    aggregate_id=lease.aggregate_id,
                    aggregate_version=lease.aggregate_version,
                    idempotency_key=lease.envelope.idempotency_key,
                )
            )
    assert await formal.PostgresInspectionWorkItemRepository(
        pool=pool
    ).finish_publication_consumer_run_if_idle(
        task_id=ids["task"], consumer_agent_run_id=consumer_run_id
    )
    # 实际终态评估与封存都在独立回滚事务中验证，清理不需要停用封存触发器。
    transaction = connection.transaction()
    await transaction.start()
    try:
        local_pool = _ConnectionPool(connection)
        finalization = formal.TaskFinalizationService(
            formal.PostgresTaskFinalizationRepository(pool=local_pool)
        )
        initial = await finalization.get(task_id=ids["task"])
        drained = await finalization.begin_exploration_draining(
            BeginExplorationDrainingCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                expected_row_version=initial.row_version,
            )
        )
        session = await finalization.begin_session_draining(
            BeginSessionDrainingCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                expected_finalization_epoch=0,
                expected_row_version=drained.row_version,
            )
        )
        await connection.execute(
            "INSERT INTO qc_web_exploration_budget(agent_run_id,site_id,task_id,session_id,"
            "target_origin_sha256,catalog_version,catalog_sha256,state,sealed_at) "
            "VALUES($1::uuid,'final-local',$2::uuid,$3,$4,'fixture',$4,'sealed',now())",
            ids["run"],
            ids["task"],
            ids["session"],
            "a" * 64,
        )
        # 复用既有正式测试的终态探索账本夹具，不把空账本冒称已完成探索。
        await connection.execute(
            "INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,"
            "source_capture_id,protocol_version,payload_hash,work_payload,status,"
            "deadline,finished_at) "
            "VALUES($1,'exploration',$2::uuid,$3,'final-local',$4::uuid,'v1',$5,'{}'::jsonb,"
            "'succeeded',now()+interval '1 minute',now())",
            "final-local:" + ids["run"],
            ids["task"],
            ids["session"],
            ids["capture"],
            "c" * 64,
        )
        snapshots = formal.PostgresWebCoverageSnapshotRepository(pool=local_pool)
        reconciliation = formal.WebInspectionReconciliation(
            state_reader=snapshots,
            snapshot_repository=snapshots,
            gap_rule_reader=snapshots,
            manifest_finalizer=None,
            publication_publisher=None,
            final_handoff=object(),
            active_session_drainer=object(),
            finalization=finalization,
            rule_execution_service=formal.RuleExecutionService(
                repository=formal.PostgresTaskRepository(pool=local_pool)
            ),
        )
        reconciled = await reconciliation.reconcile(
            formal.ReconcileWebInspectionCommand(
                task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
            )
        )
        assert reconciled.status == "gate_ready", reconciled
        result = await finalization.evaluate_and_seal(
            EvaluateCompletionGateCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                expected_finalization_epoch=0,
                expected_row_version=session.row_version,
                execution_status=TaskExecutionStatus.COMPLETED_WITH_GAPS,
                quality_outcome=TaskQualityOutcome.PASS,
            )
        )
        assert result.snapshot.gate_passed is True, result.snapshot.block_reasons
        assert (
            await connection.fetchval(
                "SELECT finalization_state FROM qc_task WHERE id=$1::uuid", ids["task"]
            )
            == "sealed"
        )
    finally:
        await transaction.rollback()
