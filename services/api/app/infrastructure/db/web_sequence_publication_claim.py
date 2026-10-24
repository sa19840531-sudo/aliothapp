"""原 Publication 队列中已认证 Web 序列的窄领取分支。"""

from datetime import UTC, datetime
from uuid import uuid4

from services.api.app.application.h5_inspection_publication import (
    H5InspectionPublicationClaimDisposition as Disposition,
)
from services.api.app.application.h5_inspection_publication import (
    H5InspectionPublicationClaimResult as ClaimResult,
)
from services.api.app.application.h5_inspection_publication import (
    H5InspectionPublicationLease,
)


async def claim_web_sequence_on_connection(
    repository, connection, *, publication_id, task_id, message_version, lease_owner, lease_seconds
):
    """仅在旧稳定 JOIN 无行后执行，复用调用方事务和 task-finalization 锁。"""
    sequence_id = await connection.fetchval(
        "SELECT web_evidence_sequence_id::text FROM qc_h5_inspection_publication "
        "WHERE id=$1::uuid AND task_id=$2::uuid",
        publication_id,
        task_id,
    )
    if sequence_id is None:
        return None
    row = await load_web_sequence_publication(
        connection, publication_id=publication_id, task_id=task_id
    )
    if row is None or not row["authorized"] or row["row_version"] != message_version:
        return ClaimResult(disposition=Disposition.STALE)
    if row["status"] not in ("pending", "leased", "retry_wait"):
        return ClaimResult(disposition=Disposition.COMPLETED)
    now = datetime.now(UTC)
    if (row["status"] == "leased" and row["lease_expires_at"] > now) or (
        row["status"] == "retry_wait" and row["next_attempt_at"] > now
    ):
        return ClaimResult(disposition=Disposition.BUSY)
    if row["attempt_count"] >= row["max_attempts"]:
        await connection.execute(
            "UPDATE qc_h5_inspection_publication SET status='failed',"
            "failure_code='attempts_exhausted',finished_at=now(),lease_owner=NULL,lease_token=NULL,"
            "lease_expires_at=NULL,row_version=row_version+1 WHERE id=$1::uuid",
            publication_id,
        )
        return ClaimResult(disposition=Disposition.COMPLETED)
    consumer = await repository._ensure_consumer_run(
        connection, task_id=task_id, finalization_epoch=row["finalization_epoch"]
    )
    token = str(uuid4())
    await connection.execute(
        "UPDATE qc_h5_inspection_publication SET status='leased',lease_owner=$2,"
        "lease_token=$3::uuid,lease_expires_at=now()+make_interval(secs=>$4),consumer_agent_run_id=$5::uuid,"
        "attempt_count=attempt_count+1,updated_at=now() WHERE id=$1::uuid",
        publication_id,
        lease_owner,
        token,
        lease_seconds,
        consumer,
    )
    return ClaimResult(
        disposition=Disposition.CLAIMED,
        lease=H5InspectionPublicationLease(
            publication_id=publication_id,
            task_id=task_id,
            source_agent_run_id=str(row["source_agent_run_id"]),
            consumer_agent_run_id=consumer,
            page_capture_id=str(row["page_capture_id"]),
            page_type_assessment_id=None,
            assessment_hash=None,
            ruleset_id=str(row["ruleset_id"]),
            ruleset_version=row["ruleset_version"],
            ruleset_snapshot_hash=row["ruleset_snapshot_hash"],
            finalization_epoch=row["finalization_epoch"],
            page_type=None,
            page_state=row["readiness_status"],
            lease_token=token,
            row_version=row["row_version"],
            attempt_count=row["attempt_count"] + 1,
            max_attempts=row["max_attempts"],
            web_evidence_sequence_id=sequence_id,
        ),
    )


async def load_web_sequence_publication(connection, *, publication_id, task_id):
    """调用方持有 task-finalization 锁后复核同一真实序列发布身份。"""
    return await connection.fetchrow(
        """
        SELECT publication.*, sequence.readiness_status, sequence.sequence_payload, sequence.sequence_hash,
            session.status AS session_status,
            (task.target_platform='web' AND task.task_config->>'workflow_version'='v2'
                AND task.task_config->>'execution_mode'='full_inspection'
                AND task.task_config->>'web_inspection_notification_mode'='enforced'
                AND task.status='running' AND task.finalization_state IN ('open','exploration_draining','session_draining')
                AND task.finalization_epoch=publication.finalization_epoch
                AND task.ruleset_id=publication.ruleset_id AND ruleset.version=publication.ruleset_version
                AND ruleset.snapshot_hash IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
                AND run.agent_type='main' AND run.status IN ('running','done','failed','aborted')
                AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                    AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                AND session.session_id='web:'||run.id::text AND session.session_owner='web-v2-workflow'
                AND session.surface_kind='web' AND session.finalization_epoch=task.finalization_epoch
                AND session.status IN ('active','draining','closing','closed')) IS TRUE AS authorized
        FROM qc_h5_inspection_publication publication
        JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=publication.web_evidence_sequence_id
            AND sequence.task_id=publication.task_id AND sequence.source_agent_run_id=publication.source_agent_run_id
            AND sequence.finalization_epoch=publication.finalization_epoch AND sequence.anchor_capture_id=publication.page_capture_id
        JOIN qc_task task ON task.id=publication.task_id
        JOIN qc_ruleset ruleset ON ruleset.id=task.ruleset_id
        JOIN qc_agent_run run ON run.id=sequence.source_agent_run_id AND run.task_id=task.id
        JOIN qc_surface_session session ON session.session_id=sequence.session_id AND session.task_id=task.id
        JOIN qc_web_run_policy_snapshot policy ON policy.task_id=task.id AND policy.agent_run_id=run.id
        WHERE publication.id=$1::uuid AND publication.task_id=$2::uuid
        FOR UPDATE OF publication
        """,
        publication_id,
        task_id,
    )
