"""同随机任务内验证序列门禁持久绑定；每次破坏均回滚，不停用生产约束。"""

import asyncpg

from services.api.app.infrastructure.db.web_sequence_completion_gate_sql import (
    WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL,
)


async def audit_sequence_gate(
    connection, *, task_id, publication_id, consumer_run_id, selected=True
):
    sql = (
        "SELECT ("
        + WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL
        + ") AS valid "
        + (
            "FROM qc_h5_inspection_publication publication JOIN qc_task task ON "
            "task.id=publication.task_id "
            "WHERE publication.id=$1::uuid AND task.id=$2::uuid"
        )
    )

    async def valid():
        return await connection.fetchval(sql, publication_id, task_id)

    assert await valid() is True
    mutations = {
        "non_anchor_time_drift": (
            "UPDATE qc_capture SET captured_at=captured_at+interval '1 second' WHERE id="
            "(SELECT capture_id FROM qc_web_evidence_sequence_member WHERE sequence_id="
            "(SELECT web_evidence_sequence_id FROM qc_h5_inspection_publication WHERE id=$1::uuid) "
            "ORDER BY ordinal LIMIT 1)",
            (publication_id,),
        ),
        "non_anchor_parent_drift": (
            "UPDATE qc_capture SET parent_capture_id=NULL WHERE id="
            "(SELECT capture_id FROM qc_web_evidence_sequence_member WHERE sequence_id="
            "(SELECT web_evidence_sequence_id FROM qc_h5_inspection_publication WHERE id=$1::uuid) "
            "ORDER BY ordinal LIMIT 1)",
            (publication_id,),
        ),
        "legacy_canonical_status_overwrite": (
            "UPDATE qc_page_capture SET metadata='{}'::jsonb,surface_session_id=NULL,"
            "parent_page_capture_id=NULL,action_execution_id=NULL,capture_role=NULL,"
            "session_command_sequence=NULL,captured_at=captured_at+interval '1 second',"
            "inspection_status='completed' WHERE id="
            "(SELECT page_capture_id FROM qc_h5_inspection_publication WHERE id=$1::uuid)",
            (publication_id,),
        ),
        "missing_capture_epoch": (
            "UPDATE qc_capture SET metadata=metadata-'finalization_epoch' WHERE id="
            "(SELECT page_capture_id FROM qc_h5_inspection_publication WHERE id=$1::uuid)",
            (publication_id,),
        ),
        "capture_time_drift": (
            "UPDATE qc_capture SET captured_at=captured_at+interval '1 second' WHERE id="
            "(SELECT page_capture_id FROM qc_h5_inspection_publication WHERE id=$1::uuid)",
            (publication_id,),
        ),
        "capture_session_drift": (
            "UPDATE qc_capture SET surface_session_id=NULL WHERE id="
            "(SELECT page_capture_id FROM qc_h5_inspection_publication WHERE id=$1::uuid)",
            (publication_id,),
        ),
        "missing_input": (
            "UPDATE qc_h5_inspection_publication_rule SET "
            "execution_plan=execution_plan-'evidence_input_set_id' WHERE publication_id=$1::uuid",
            (publication_id,),
        ),
        "fake_input": (
            "UPDATE qc_h5_inspection_publication_rule SET "
            "execution_plan=jsonb_set(execution_plan,'{evidence_input_set_id}',"
            "'\"fake-input\"') WHERE publication_id=$1::uuid",
            (publication_id,),
        ),
        "contract": (
            "UPDATE qc_h5_inspection_publication_rule SET contract_hash=repeat('0',64) "
            "WHERE publication_id=$1::uuid",
            (publication_id,),
        ),
        "missing_selection": (
            "UPDATE qc_h5_inspection_publication_rule SET "
            "execution_plan=execution_plan-'selection' WHERE publication_id=$1::uuid",
            (publication_id,),
        ),
        "wrong_run": (
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            (task_id, consumer_run_id),
        ),
        "wrong_epoch": (
            "UPDATE qc_task SET finalization_epoch=finalization_epoch+1 WHERE id=$1::uuid",
            (task_id,),
        ),
        "wrong_anchor": (
            "UPDATE qc_h5_inspection_publication SET page_capture_id=(SELECT capture_id "
            "FROM qc_web_evidence_sequence_member WHERE "
            "sequence_id=web_evidence_sequence_id ORDER BY ordinal LIMIT 1) WHERE id=$1::uuid",
            (publication_id,),
        ),
        "artifact_hash": (
            "UPDATE qc_evidence_artifact SET checksum='sha256:'||repeat('0',64) WHERE "
            "task_id=$1::uuid",
            (task_id,),
        ),
        "nonterminal_work": (
            "UPDATE qc_inspection_work_item SET status='pending' WHERE task_id=$1::uuid",
            (task_id,),
        ),
        "orphan_work": (
            "DELETE FROM qc_h5_inspection_publication_work_item WHERE publication_id=$1::uuid",
            (publication_id,),
        ),
    }
    if not selected:
        mutations = {
            name: mutation
            for name, mutation in mutations.items()
            if name
            in {
                "legacy_canonical_status_overwrite",
                "missing_capture_epoch",
                "capture_time_drift",
                "capture_session_drift",
                "wrong_run",
                "wrong_epoch",
                "non_anchor_time_drift",
                "non_anchor_parent_drift",
            }
        }
    results = {}
    for name, (statement, parameters) in mutations.items():
        transaction = connection.transaction()
        await transaction.start()
        try:
            try:
                await connection.execute(statement, *parameters)
            except (
                asyncpg.CheckViolationError,
                asyncpg.ForeignKeyViolationError,
                asyncpg.RestrictViolationError,
            ):
                results[name] = "database_constraint_rejected"
            else:
                assert await valid() is False, name
                results[name] = "gate_rejected"
        finally:
            await transaction.rollback()
        assert await valid() is True, name
    if selected:
        assert results["missing_input"] == results["fake_input"] == "gate_rejected"
    print("R34_NEGATIVE_RESULTS", results)
    from datetime import UTC, datetime

    from services.api.app.domain.evidence import (
        EvidenceQualityStatus,
        build_evidence_manifest_reference,
    )
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.db.web_payload_readiness_repository import _ConnectionPool

    repository = PostgresEvidenceManifestRepository(pool=_ConnectionPool(connection))
    capture_id = await connection.fetchval(
        "SELECT anchor_capture_id::text FROM qc_web_evidence_sequence "
        "WHERE sequence_id=(SELECT web_evidence_sequence_id FROM "
        "qc_h5_inspection_publication WHERE id=$1::uuid)",
        publication_id,
    )
    previous = await repository.get_latest_for_capture(task_id=task_id, capture_id=capture_id)
    transaction = connection.transaction()
    await transaction.start()
    try:
        await repository.add(
            manifest=build_evidence_manifest_reference(
                task_id=task_id,
                session_id=previous.session_id,
                capture_id=capture_id,
                manifest_version=previous.manifest_version + 1,
                artifacts=previous.artifacts,
                quality_status=EvidenceQualityStatus.PENDING,
                quality_reason="pending_verification",
                created_at=datetime.now(UTC),
            )
        )
        assert await valid() is True
        assert (
            await repository.get(manifest_id=previous.manifest_id)
        ).manifest_hash == previous.manifest_hash
    finally:
        await transaction.rollback()
