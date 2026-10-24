"""检查点之间只接受实际 Capture 父链及权威等价恢复账本。"""

import json

from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.inspection import EvidenceRequest
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.restoration_attempt_repository import _parse_attempt


def _object(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


async def _capture(connection, identity):
    return await connection.fetchrow(
        "SELECT capture.id::text,capture.task_id::text,capture.agent_run_id::text,"
        "capture.surface_session_id AS session_id,capture.parent_capture_id::text,"
        "capture.capture_status AS status,capture.captured_at,"
        "(capture.metadata->>'finalization_epoch')::int AS epoch,manifest.manifest_id,"
        "manifest.manifest_version,manifest.manifest_hash,manifest.quality_status,"
        "manifest.manifest_payload,capture.capture_role,"
        "(SELECT coalesce(jsonb_agg(jsonb_build_object('id',artifact.id::text,"
        "'artifact_type',artifact.artifact_type,'checksum',artifact.checksum,"
        "'storage_path',artifact.storage_path)),'[]'::jsonb) FROM qc_evidence_artifact AS artifact "
        "WHERE artifact.page_capture_id=capture.id "
        "AND artifact.task_id=capture.task_id) AS artifacts "
        "FROM qc_capture AS capture LEFT JOIN LATERAL (SELECT * FROM qc_evidence_manifest "
        "WHERE capture_id=capture.id ORDER BY manifest_version DESC LIMIT 1) AS manifest "
        "ON true WHERE capture.id=$1::uuid",
        identity,
    )


def _valid_capture(row, *, task_id, agent_run_id, epoch, before):
    valid = bool(
        row
        and row["task_id"] == task_id
        and row["agent_run_id"] == agent_run_id
        and row["epoch"] == epoch
        and row["status"] == "captured"
        and row["quality_status"] == "pass"
        and row["manifest_id"]
        and row["captured_at"] is not None
        and row["captured_at"] <= before
    )
    if not valid:
        return False
    try:
        manifest = EvidenceManifestReference.from_dict(_object(row["manifest_payload"]))
        stored = (
            json.loads(row["artifacts"]) if isinstance(row["artifacts"], str) else row["artifacts"]
        )
        actual = {item["id"]: item for item in stored}
        return (
            manifest.task_id == task_id
            and manifest.session_id == row["session_id"]
            and manifest.capture_id == row["id"]
            and manifest.manifest_id == row["manifest_id"]
            and manifest.manifest_hash == row["manifest_hash"]
            and manifest.manifest_version == row["manifest_version"]
            and manifest.quality_status.value == "pass"
            and bool(actual)
            and len(actual) == len(manifest.artifacts)
            and all(
                item.artifact_id in actual
                and item.role == row["capture_role"]
                and all(
                    actual[item.artifact_id][key] == getattr(item, key)
                    for key in ("checksum", "storage_path", "artifact_type")
                )
                for item in manifest.artifacts
            )
        )
    except (ValueError, KeyError, TypeError):
        return False


async def load_checkpoint_lineage(
    connection,
    *,
    task_id,
    agent_run_id,
    epoch,
    source_capture_id,
    origin_capture_id,
    recovery_policy,
    before,
    deadline,
    allow_closed_epoch=False,
):
    """有界追溯，不靠同 URL、会话名或自报等价标签接受恢复。"""
    if allow_closed_epoch:
        from services.api.app.infrastructure.db.journey_epoch import closed_journey_epoch_is_bound

        if not await closed_journey_epoch_is_bound(
            connection,
            task_id=task_id,
            agent_run_id=agent_run_id,
            epoch=epoch,
        ):
            return None
    references, restorations, seen = [], [], set()
    cursor, session_id = source_capture_id, None
    boundary = min(before, deadline)
    for _ in range(80):
        if cursor is None or cursor in seen:
            return None
        seen.add(cursor)
        row = await _capture(connection, cursor)
        if not _valid_capture(
            row, task_id=task_id, agent_run_id=agent_run_id, epoch=epoch, before=boundary
        ):
            return None
        if session_id is not None and row["session_id"] != session_id:
            return None
        references.append(
            {
                key: row[key]
                for key in ("id", "session_id", "manifest_id", "manifest_version", "manifest_hash")
            }
        )
        if cursor == origin_capture_id:
            return {"captures": references, "restorations": restorations}
        rows = await connection.fetch(
            "SELECT restoration.*,"
            "request.request_payload FROM qc_restoration_attempt AS restoration "
            "JOIN qc_evidence_request AS request ON request.request_id=restoration.request_id "
            "JOIN qc_task AS task ON task.id=restoration.task_id "
            "WHERE restoration.restored_capture_id=$1::uuid AND restoration.task_id=$2::uuid "
            "AND restoration.finalization_epoch=$3 AND request.finalization_epoch=$3 "
            "AND (task.finalization_epoch=$3 OR $4::boolean)",
            cursor,
            task_id,
            epoch,
            allow_closed_epoch,
        )
        if len(rows) > 1:
            return None
        if rows:
            if recovery_policy == "blocked":
                return None
            raw = dict(rows[0])
            if raw["restored_capture_id"] is not None:
                raw["restored_capture_id"] = str(raw["restored_capture_id"])
            try:
                attempt = _parse_attempt(raw)
                request = EvidenceRequest.from_dict(_object(raw["request_payload"]))
            except (ValueError, KeyError, TypeError):
                return None
            plan, assessment = attempt.plan, attempt.assessment
            if (
                attempt.status.value != "equivalent"
                or assessment is None
                or not assessment.passed
                or plan.task_id != task_id
                or plan.target_session_id != row["session_id"]
                or request.task_id != task_id
                or plan.request_id != request.request_id
                or request.target_capture_id != plan.original_capture_id
                or request.side_effect_policy.value != "read_only"
                or assessment.assessment_id != raw["equivalence_assessment_id"]
                or stable_protocol_hash(assessment.to_dict()) != raw["assessment_hash"]
                or plan.plan_hash != raw["plan_hash"]
                or plan.plan_id != raw["plan_id"]
                or assessment.required_equivalence != request.required_equivalence
                or assessment.minimum_source_grade != request.minimum_source_grade
                or not any(item.dimension.value == "business" for item in assessment.comparisons)
                or attempt.finished_at is None
                or not attempt.started_at <= attempt.finished_at <= boundary
                or attempt.finished_at > plan.deadline
                or plan.deadline > request.expires_at
                or plan.deadline > deadline
                or (
                    plan.source_session_id != plan.target_session_id
                    and recovery_policy != "new_instance"
                )
            ):
                return None
            if (
                plan.source_session_id != plan.target_session_id
                and not attempt.action_execution_ids
            ):
                return None
            last_time = attempt.started_at
            actions = []
            for action_id in attempt.action_execution_ids:
                action = await connection.fetchrow(
                    "SELECT * FROM qc_action_execution WHERE action_execution_id=$1", action_id
                )
                if (
                    action is None
                    or str(action["task_id"]) != task_id
                    or action["session_id"] != plan.target_session_id
                    or action["finalization_epoch"] != epoch
                    or action["status"] != "completed"
                    or action["started_at"] is None
                    or action["action_finished_at"] is None
                    or not last_time
                    <= action["started_at"]
                    <= action["action_finished_at"]
                    <= attempt.finished_at
                    or (_object(action["intent_payload"]).get("parameters") or {}).get(
                        "restoration_plan_id"
                    )
                    != plan.plan_id
                ):
                    return None
                last_time = action["action_finished_at"]
                actions.append(action_id)
            if actions and str(action["result_capture_id"]) != cursor:
                return None
            restorations.append(
                {
                    "attempt_id": attempt.attempt_id,
                    "plan_id": plan.plan_id,
                    "plan_hash": plan.plan_hash,
                    "assessment_id": assessment.assessment_id,
                    "source_grade": assessment.source_grade.value,
                    "source_session_id": plan.source_session_id,
                    "target_session_id": plan.target_session_id,
                    "original_capture_id": plan.original_capture_id,
                    "restored_capture_id": cursor,
                    "action_execution_ids": actions,
                }
            )
            cursor, session_id, boundary = (
                plan.original_capture_id,
                plan.source_session_id,
                attempt.started_at,
            )
        else:
            cursor, session_id, boundary = (
                row["parent_capture_id"],
                row["session_id"],
                row["captured_at"],
            )
    return None
