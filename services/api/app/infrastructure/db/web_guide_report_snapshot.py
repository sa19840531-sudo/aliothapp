"""当前 Web Run 的引导支线阶段与补充证据只读投影。"""

from __future__ import annotations

import json


async def read_web_guide_page_snapshot(connection, *, task_id, agent_run_id, epoch):
    """按阶段来源、父子 Capture、Run 和 Epoch 同时约束引导证据。"""
    rows = await connection.fetch(
        "SELECT phase.source_capture_id::text AS source_capture_id,"
        "phase.context_payload->>'candidate_id' AS candidate_id,"
        "phase.context_payload->>'destination_url' AS requested_url,"
        "phase.status,phase.terminal_reason_code AS reason_code,"
        "step.decision_status,child.id::text AS capture_id,child.page_type,"
        "guide_manifest.quality_status AS manifest_quality_status,"
        "child.url AS final_url,shot.id::text AS screenshot_artifact_id,"
        "(SELECT jsonb_agg(ref.id::text ORDER BY ref.created_at,ref.id) FROM "
        "(SELECT artifact.id,artifact.created_at FROM qc_evidence_artifact artifact "
        "WHERE artifact.task_id=phase.task_id AND artifact.page_capture_id=child.id "
        "AND phase.evidence_artifact_ids ? artifact.id::text "
        "ORDER BY artifact.created_at,artifact.id LIMIT 101) ref) AS artifact_ids "
        "FROM qc_surface_phase_run phase "
        "JOIN qc_capture source_capture ON source_capture.id=phase.source_capture_id "
        "AND source_capture.task_id=phase.task_id "
        "AND source_capture.surface_session_id=phase.session_id "
        "LEFT JOIN LATERAL (SELECT decision_status,result_capture_id "
        "FROM qc_surface_phase_step WHERE phase_run_id=phase.phase_run_id "
        "AND step_key='terminal' ORDER BY step_index DESC LIMIT 1) step ON true "
        "LEFT JOIN qc_page_capture child ON child.id=step.result_capture_id "
        "AND child.task_id=phase.task_id AND child.agent_run_id=source_capture.agent_run_id "
        "AND child.parent_page_capture_id=phase.source_capture_id "
        "AND child.metadata->>'capture_purpose'='web_guide_page' "
        "AND EXISTS(SELECT 1 FROM qc_capture child_capture "
        "WHERE child_capture.id=child.id AND child_capture.task_id=child.task_id "
        "AND child_capture.capture_role='supplemental') "
        "LEFT JOIN LATERAL (SELECT manifest.quality_status "
        "FROM qc_evidence_manifest manifest "
        "WHERE manifest.task_id=phase.task_id AND manifest.capture_id=child.id "
        "ORDER BY manifest.manifest_version DESC LIMIT 1) guide_manifest ON true "
        "LEFT JOIN LATERAL (SELECT artifact.id FROM qc_evidence_artifact artifact "
        "WHERE artifact.task_id=child.task_id AND artifact.page_capture_id=child.id "
        "AND artifact.artifact_type='screenshot' "
        "AND phase.evidence_artifact_ids ? artifact.id::text "
        "ORDER BY artifact.created_at,artifact.id LIMIT 1) shot ON true "
        "WHERE phase.task_id=$1::uuid AND source_capture.agent_run_id=$2::uuid "
        "AND phase.finalization_epoch=$3 "
        "AND source_capture.metadata->>'finalization_epoch'=$3::integer::text "
        "AND phase.phase_key='web_guide_page' "
        "AND phase.phase_protocol_version='web-guide-page.v1' "
        "ORDER BY phase.started_at,phase.phase_run_id LIMIT 101",
        task_id, agent_run_id, epoch,
    )
    if len(rows) > 100:
        raise ValueError("WEB_GUIDE_REPORT_LIMIT_EXCEEDED")
    result = []
    for raw in rows:
        row = dict(raw)
        artifact_ids = row.get("artifact_ids") or []
        if isinstance(artifact_ids, str):
            artifact_ids = json.loads(artifact_ids)
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 100:
            raise ValueError("WEB_GUIDE_REPORT_REFERENCE_LIMIT_EXCEEDED")
        capture_valid = bool(
            row.get("capture_id") and row.get("screenshot_artifact_id")
            and row.get("manifest_quality_status") == "pass"
        )
        if row.get("status") == "completed" and row.get("decision_status") == "skipped":
            status = "skipped"
        elif row.get("status") == "completed" and capture_valid:
            status = (
                "confirmed" if row.get("decision_status") == "confirmed"
                and row.get("page_type") == "guide_page" else "uncertain"
            )
        else:
            status = "incomplete"
        reason_code = row.get("reason_code")
        if row.get("capture_id") and not capture_valid:
            reason_code = "WEB_GUIDE_EVIDENCE_NOT_READY"
        result.append({
            "source_capture_id": row.get("source_capture_id"),
            "candidate_id": row.get("candidate_id"),
            "status": status,
            "reason_code": reason_code,
            "capture_id": row.get("capture_id") if capture_valid else None,
            "page_type": "guide_page" if status == "confirmed" else None,
            "coverage_group": "evidence_only",
            "requested_url": row.get("requested_url"),
            "final_url": row.get("final_url") if capture_valid else None,
            "screenshot_artifact_id": row.get("screenshot_artifact_id") if capture_valid else None,
            "artifact_ids": artifact_ids if capture_valid else [],
        })
    return result
