"""新 Web 正式正文保留当前周期可信逻辑页和主截图，历史快照不补写。"""

import json

from services.api.app.application.report_insight import _web_report_logical_pages
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError
from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
    task_config_of_sql,
)
from services.api.app.infrastructure.db.web_capture_identity import read_web_capture_identities

# 经过的页面只留认路记录，不进报告的逻辑页和广告页列表
_PAGE_IN_INSPECTION_SCOPE_SQL = page_type_in_inspection_scope_sql(
    task_config_of_sql("c.task_id"), "p.page_type"
)


async def read_web_report_page_snapshot(connection, *, task_id, agent_run_id, epoch):
    try:
        identities = await read_web_capture_identities(
            connection, task_id=task_id, agent_run_id=agent_run_id
        )
    except WebRunPolicyError as error:
        return [], [], [{"capture_id": "history", "reason_code": str(error)}]
    rows = await connection.fetch(
        "SELECT p.id::text AS page_capture_id,p.url,p.page_type,p.page_seq,"
        "p.inspection_status,c.captured_at, "
        "c.capture_role,p.parent_page_capture_id::text AS parent_page_capture_id, "
        "p.metadata->>'capture_purpose' AS capture_purpose,"
        "p.metadata->>'requested_url' AS requested_url,"
        "p.metadata->'menu_entry_ids' AS menu_entry_ids, "
        "p.metadata->'web_page_type_assessment' AS assessment, "
        "(SELECT a.id::text FROM qc_evidence_artifact a "
        "WHERE a.task_id=p.task_id AND a.page_capture_id=p.id "
        "AND a.artifact_type='screenshot' "
        "AND (COALESCE(p.metadata->>'capture_purpose','')<>'web_ad_page' "
        "OR a.metadata->>'purpose'='ad_landing') "
        "ORDER BY a.created_at,a.id LIMIT 1) "
        "AS screenshot_artifact_id, "
        "(SELECT jsonb_agg(ref.id::text ORDER BY ref.created_at,ref.id) FROM "
        "(SELECT a.id,a.created_at FROM qc_evidence_artifact a WHERE a.task_id=p.task_id "
        "AND a.page_capture_id=p.id ORDER BY a.created_at,a.id LIMIT 101) ref) AS artifact_ids, "
        "CASE WHEN supplemental_phase.phase_run_id IS NULL THEN "
        "jsonb_build_object('status','not_attempted','capture_id',NULL,"
        "'screenshot_artifact_id',NULL,'reason_code',NULL) "
        "WHEN supplemental_phase.status='completed' "
        "AND supplemental.capture_id IS NOT NULL "
        "AND supplemental.screenshot_artifact_id IS NOT NULL THEN "
        "jsonb_build_object('status','captured','capture_id',supplemental.capture_id,"
        "'screenshot_artifact_id',supplemental.screenshot_artifact_id,"
        "'reason_code',supplemental_phase.terminal_reason_code) "
        "ELSE jsonb_build_object('status','failed','capture_id',NULL,"
        "'screenshot_artifact_id',NULL,'reason_code',COALESCE("
        "supplemental_phase.terminal_reason_code,"
        "'WEB_FULL_PAGE_SCREENSHOT_SUPPLEMENTAL_UNAVAILABLE')) "
        "END AS supplemental_full_page "
        "FROM qc_page_capture p JOIN qc_capture c ON c.id=p.id AND c.task_id=p.task_id "
        "LEFT JOIN LATERAL (SELECT phase.phase_run_id,phase.status,phase.terminal_reason_code,"
        "phase.evidence_artifact_ids FROM qc_surface_phase_run phase "
        "WHERE phase.task_id=p.task_id AND phase.session_id=c.surface_session_id "
        "AND phase.source_capture_id=p.id AND phase.phase_key='web_full_page_supplemental' "
        "AND phase.phase_protocol_version='web-full-page-supplemental.v1' "
        "ORDER BY phase.started_at,phase.phase_run_id LIMIT 1) supplemental_phase ON true "
        "LEFT JOIN LATERAL (SELECT child.id::text AS capture_id,"
        "artifact.id::text AS screenshot_artifact_id "
        "FROM qc_page_capture child JOIN qc_capture child_capture "
        "ON child_capture.id=child.id AND child_capture.task_id=child.task_id "
        "JOIN qc_evidence_artifact artifact ON artifact.task_id=child.task_id "
        "AND artifact.page_capture_id=child.id AND artifact.artifact_type='screenshot' "
        "WHERE child.task_id=p.task_id AND child.agent_run_id=p.agent_run_id "
        "AND child.parent_page_capture_id=p.id AND child_capture.capture_role='supplemental' "
        "AND child.metadata->>'capture_purpose'='web_full_page_supplemental' "
        "AND supplemental_phase.evidence_artifact_ids ? artifact.id::text "
        "ORDER BY child.captured_at,child.id,artifact.created_at,artifact.id LIMIT 1) "
        "supplemental ON true "
        "WHERE c.task_id=$1::uuid AND c.agent_run_id=$2::uuid "
        "AND (c.capture_role<>'supplemental' OR ("
        "c.capture_role='supplemental' AND p.metadata->>'capture_purpose'='web_ad_page' "
        "AND p.page_type='ad_page')) "
        "AND c.metadata->>'finalization_epoch'=$3::integer::text "
        f"AND {_PAGE_IN_INSPECTION_SCOPE_SQL} "
        "ORDER BY p.page_seq,p.id LIMIT 1001",
        task_id,
        agent_run_id,
        epoch,
    )
    if len(rows) > 1000:
        raise ValueError("WEB_REPORT_PAGE_LIMIT_EXCEEDED")
    pages, assessments, ad_pages, diagnostics = [], [], [], []
    for row in rows:
        page = dict(row)
        for key in ("assessment", "menu_entry_ids", "artifact_ids", "supplemental_full_page"):
            if isinstance(page.get(key), str):
                page[key] = json.loads(page[key])
        for key in ("menu_entry_ids", "artifact_ids"):
            references = page.get(key) or []
            if (
                not isinstance(references, list)
                or len(references) > 100
                or any(not isinstance(item, str) or len(item) > 200 for item in references)
            ):
                raise ValueError("WEB_REPORT_PAGE_REFERENCE_LIMIT_EXCEEDED")
        assessment = page.pop("assessment") or {}
        if page.get("capture_role") == "supplemental":
            ad_pages.append(
                {
                    "capture_id": page["page_capture_id"],
                    "parent_capture_id": page.get("parent_page_capture_id"),
                    "capture_purpose": "web_ad_page",
                    "page_type": "ad_page",
                    "coverage_group": "evidence_only",
                    "requested_url": page.get("requested_url"),
                    "final_url": page.get("url"),
                    "captured_at": page.get("captured_at"),
                    "screenshot_artifact_id": page.get("screenshot_artifact_id"),
                    "artifact_ids": list(page.get("artifact_ids") or []),
                }
            )
            continue
        # 冻结页型和固定原因，不把原始模型诊断正文写入新增快照。
        assessment.pop("diagnostic_message", None)
        assessments.append({**assessment, "page_capture_id": page["page_capture_id"]})
        pages.append(page)
        identity = identities.get(page["page_capture_id"], {})
        if identity.get("logical_page_identity_status") != "verified":
            diagnostics.append(
                {
                    "capture_id": page["page_capture_id"],
                    "reason_code": identity.get(
                        "logical_page_identity_status", "WEB_LOGICAL_ID_UNTRUSTED"
                    ),
                }
            )
    _, logical_pages = _web_report_logical_pages(pages, assessments, trusted_identities=identities)
    assessment_by_capture = {item["page_capture_id"]: item for item in assessments}
    for page in logical_pages:
        frozen = assessment_by_capture.get(page["primary_capture_id"], {})
        for key in (
            "subject_region_facts", "structure_fingerprint", "guide_version",
            "decision_source", "model_result", "deterministic_result",
        ):
            if key in frozen:
                page[key] = frozen[key]
    return logical_pages, ad_pages, diagnostics
