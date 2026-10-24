"""Web 分类竞态的有界等待，复用原公告版本与 Outbox，不重开终态。"""

from datetime import UTC, datetime

from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)
from services.api.app.infrastructure.db.web_sequence_publication_claim import (
    load_web_sequence_publication,
)


async def may_wait_for_classification(connection, lease, sequence):
    context = await connection.fetchrow(
        "SELECT task.finalization_state,run.status FROM qc_task task JOIN qc_agent_run run ON run.task_id=task.id WHERE task.id=$1::uuid AND run.id=$2::uuid",
        lease.task_id,
        lease.source_agent_run_id,
    )
    if (
        context is None
        or context["finalization_state"] != "open"
        or context["status"] not in {"running", "pending"}
    ):
        return False
    snapshot = await read_web_policy_on_connection(
        connection, lease.task_id, lease.source_agent_run_id
    )
    if snapshot is None or snapshot.payload_sha256 != sequence.get("policy_hash"):
        return False
    try:
        return materialize_web_policy(snapshot).deadline > datetime.now(UTC)
    except ValueError:
        return False


async def defer_classification(repository, connection, lease, *, work_count, rule_count):
    version = await connection.fetchval(
        "UPDATE qc_h5_inspection_publication SET status='retry_wait',failure_code='WEB_SEQUENCE_CLASSIFICATION_PENDING',failure_message='等待同一锚点真实分类终态',attempt_count=GREATEST(attempt_count-1,0),next_attempt_at=now()+make_interval(secs=>$4),lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,row_version=row_version+1,finished_at=NULL,updated_at=now(),published_work_item_count=$5,expected_batch_count=$5,candidate_rule_count=$6 WHERE id=$1::uuid AND lease_token=$2::uuid AND row_version=$3 RETURNING row_version",
        lease.publication_id,
        lease.lease_token,
        lease.row_version,
        repository._retry_delay_seconds,
        work_count,
        rule_count,
    )
    if version is None:
        raise RuntimeError("WEB_SEQUENCE_CLASSIFICATION_WAIT_LEASE_REJECTED")
    await repository._insert_available_event(
        connection,
        publication_id=lease.publication_id,
        task_id=lease.task_id,
        finalization_epoch=lease.finalization_epoch,
        row_version=version,
        occurred_at=datetime.now(UTC),
    )


async def wake_classified_sequences(repository, *, task_id, agent_run_id, session_id, capture_id):
    pool = await repository._get_pool()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
        )
        candidates = await connection.fetch(
            "SELECT publication.id::text FROM qc_h5_inspection_publication publication JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=publication.web_evidence_sequence_id WHERE publication.task_id=$1::uuid AND publication.source_agent_run_id=$2::uuid AND publication.page_capture_id=$3::uuid AND sequence.session_id=$4 AND sequence.purpose='interaction_sequence' AND publication.status IN ('pending','retry_wait') AND EXISTS(SELECT 1 FROM qc_h5_page_type_assessment assessment WHERE assessment.task_id=publication.task_id AND assessment.agent_run_id=publication.source_agent_run_id AND assessment.page_capture_id=publication.page_capture_id)",
            task_id,
            agent_run_id,
            capture_id,
            session_id,
        )
        for candidate in candidates:
            row = await load_web_sequence_publication(
                connection, publication_id=candidate["id"], task_id=task_id
            )
            if (
                row is None
                or not row["authorized"]
                or row["status"] not in {"pending", "retry_wait"}
            ):
                continue
            version = await connection.fetchval(
                "UPDATE qc_h5_inspection_publication SET next_attempt_at=now(),row_version=row_version+1,updated_at=now() WHERE id=$1::uuid AND row_version=$2 RETURNING row_version",
                candidate["id"],
                row["row_version"],
            )
            await repository._insert_available_event(
                connection,
                publication_id=candidate["id"],
                task_id=task_id,
                finalization_epoch=row["finalization_epoch"],
                row_version=version,
                occurred_at=datetime.now(UTC),
            )
