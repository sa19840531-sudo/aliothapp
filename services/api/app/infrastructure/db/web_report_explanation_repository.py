"""Web 只读报告事实查询：固定 Task/Run/Epoch 与调用者对象权限。"""

import logging

from services.api.app.application.web_report_explanation import build_web_report_explanation
from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
    task_config_of_sql,
)

# 经过的页面没有送质检，不进报告的质检通知列表
_PAGE_IN_INSPECTION_SCOPE_SQL = page_type_in_inspection_scope_sql(
    task_config_of_sql("capture.task_id"), "page.page_type"
)


async def read_web_report_explanation(connection, *, task_id, agent_run_id, scope_sql, scope_args):
    context = await connection.fetchrow(
        "SELECT task.finalization_epoch FROM qc_task task "
        "JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$2::uuid AND run.agent_type='main' "
        "WHERE task.id=$1::uuid AND task.target_platform='web' "
        "AND task.task_config->>'workflow_version'='v2' "
        "AND NOT EXISTS(SELECT 1 FROM qc_agent_run newer WHERE newer.task_id=task.id "
        "AND newer.agent_type='main' AND (newer.created_at,newer.id)>(run.created_at,run.id)) "
        + scope_sql,
        task_id,
        agent_run_id,
        *scope_args,
    )
    if context is None:
        return None
    epoch = context["finalization_epoch"]
    args = (task_id, agent_run_id, epoch)
    notifications = await connection.fetch(
        "SELECT page.id::text AS capture_id, page.metadata->'web_inspection_notification' AS notification, "
        "page.metadata->'primary_content_capture' AS primary_content_capture, "
        "page.detection_status AS classification_status, page.page_type "
        "FROM qc_page_capture page JOIN qc_capture capture ON capture.id=page.id AND capture.task_id=page.task_id "
        "JOIN qc_surface_session session ON session.task_id=capture.task_id AND session.session_id=capture.surface_session_id "
        "WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid "
        "AND COALESCE(page.metadata->>'capture_purpose','')<>'web_guide_page' "
        "AND capture.metadata->>'finalization_epoch'=$3::integer::text "
        "AND session.session_id='web:'||$2::uuid::text AND session.session_owner='web-v2-workflow' "
        "AND session.finalization_epoch=$3 "
        f"AND {_PAGE_IN_INSPECTION_SCOPE_SQL} "
        "ORDER BY capture.captured_at,capture.id LIMIT 1001",
        *args,
    )
    sequences = await connection.fetch(
        "SELECT sequence_id::text,anchor_capture_id::text,purpose, "
        "sequence_payload->'frames' AS frames,sequence_payload->'capability_revision' AS capability_revision, "
        "sequence_payload->>'readiness_status' AS readiness_status, "
        "sequence_payload->>'readiness_exit_reason' AS readiness_exit_reason "
        "FROM qc_web_evidence_sequence WHERE task_id=$1::uuid AND source_agent_run_id=$2::uuid "
        "AND finalization_epoch=$3 ORDER BY created_at,sequence_id LIMIT 1001",
        *args,
    )
    publication_rules = await connection.fetch(
        "SELECT p.id::text AS publication_id,p.status, m.rule_id::text,m.rule_code,m.contract_hash, "
        "p.web_evidence_sequence_id::text AS sequence_id,m.execution_plan->'selection' AS selection "
        "FROM qc_h5_inspection_publication p JOIN qc_h5_inspection_publication_rule m ON m.publication_id=p.id "
        "WHERE p.task_id=$1::uuid AND p.source_agent_run_id=$2::uuid AND p.finalization_epoch=$3 "
        "ORDER BY p.created_at,p.id,m.ordinal LIMIT 1001",
        *args,
    )
    primary_rows = await connection.fetch(
        "SELECT entry.menu_entry_id,entry.diagnostic->'primary_content_diagnostics' AS diagnostics "
        "FROM qc_web_menu_ledger_entry entry JOIN qc_web_exploration_budget budget "
        "ON budget.agent_run_id=entry.agent_run_id AND budget.site_id=entry.site_id "
        "JOIN qc_surface_session session ON session.task_id=budget.task_id AND session.session_id=budget.session_id "
        "WHERE budget.task_id=$1::uuid AND budget.agent_run_id=$2::uuid "
        "AND session.session_id='web:'||$2::uuid::text AND session.session_owner='web-v2-workflow' "
        "AND session.finalization_epoch=$3 ORDER BY entry.catalog_position,entry.menu_entry_id LIMIT 1001",
        *args,
    )
    members = await connection.fetch(
        "SELECT w.id::text AS work_item_id,w.status AS work_status,m.rule_id::text,m.rule_code,m.status, "
        "m.contract_hash,m.rule_execution_id::text,m.evidence_input_set_id,i.scope AS input_scope, "
        "i.input_set_hash,i.web_evidence_sequence_id::text AS sequence_id, "
        "e.execution_status AS canonical_status,e.judge_output->'web_composite_provenance' AS canonical, "
        "m.last_error_code AS error_code, "
        "(SELECT jsonb_agg(jsonb_build_object('attempt_id',a.id,'status',a.status,'error_code',a.error_code, "
        "'consumer_agent_run_id',a.consumer_agent_run_id,'execution_status',d->>'execution_status', "
        "'observation_packet_id',d->>'observation_packet_id') ORDER BY a.attempt_no) "
        "FROM qc_inspection_attempt a CROSS JOIN LATERAL jsonb_array_elements(COALESCE(a.result_payload->'decisions','[]')) d "
        "WHERE a.work_item_id=w.id AND d->>'rule_id'=m.rule_id::text "
        "AND d->>'work_item_id'=w.id::text AND d->>'attempt_id'=a.id::text) AS attempts "
        "FROM qc_inspection_work_item w JOIN qc_inspection_work_item_rule m ON m.work_item_id=w.id "
        "LEFT JOIN qc_evidence_input_set i ON i.input_set_id=m.evidence_input_set_id AND i.task_id=w.task_id "
        "AND i.source_agent_run_id=w.producer_agent_run_id AND i.finalization_epoch=w.finalization_epoch "
        "AND i.rule_id=m.rule_id AND i.rule_contract_hash=m.contract_hash "
        "LEFT JOIN qc_rule_execution e ON e.id=m.rule_execution_id AND e.task_id=w.task_id "
        "AND e.rule_id=m.rule_id AND e.page_capture_id=w.page_capture_id "
        "WHERE w.task_id=$1::uuid AND w.producer_agent_run_id=$2::uuid AND w.finalization_epoch=$3 "
        "ORDER BY w.created_at,w.id,m.ordinal LIMIT 1001",
        *args,
    )
    import json

    rows = {}
    for key, records in (
        ("notifications", notifications),
        ("sequences", sequences),
        ("publication_rules", publication_rules),
        ("rule_materials", members),
    ):
        rows[key] = [
            {
                k: json.loads(v)
                if isinstance(v, str)
                and k
                in {
                    "notification",
                    "primary_content_capture",
                    "frames",
                    "capability_revision",
                    "selection",
                    "canonical",
                    "attempts",
                }
                else v
                for k, v in dict(row).items()
            }
            for row in records
        ]
    from services.api.app.application.web_primary_content_diagnostic import (
        primary_content_diagnostics,
    )

    if len(primary_rows) > 1000:
        raise ValueError("WEB_REPORT_EXPLANATION_LIMIT_EXCEEDED")
    rows["primary_content"] = [
        dict(item, menu_entry_id=row["menu_entry_id"])
        for row in primary_rows
        for item in primary_content_diagnostics(
            json.loads(row["diagnostics"])
            if isinstance(row["diagnostics"], str)
            else row["diagnostics"]
        )
    ]
    from services.api.app.infrastructure.db.web_guide_report_snapshot import (
        read_web_guide_page_snapshot,
    )
    from services.api.app.infrastructure.db.web_report_page_snapshot import (
        read_web_report_page_snapshot,
    )

    (
        web_logical_pages,
        web_ad_pages,
        web_identity_diagnostics,
    ) = await read_web_report_page_snapshot(
        connection, task_id=task_id, agent_run_id=agent_run_id, epoch=epoch
    )
    rows["web_logical_pages"] = web_logical_pages
    rows["web_ad_pages"] = web_ad_pages
    try:
        web_guide_pages = await read_web_guide_page_snapshot(
            connection, task_id=task_id, agent_run_id=agent_run_id, epoch=epoch
        )
    except Exception:  # noqa: BLE001 - 可选引导投影失败不能中断 Web 主报告
        logging.getLogger(__name__).warning(
            "web_guide_report_snapshot_failed task_id=%s run_id=%s",
            task_id, agent_run_id,
        )
        web_guide_pages = []
    rows["web_guide_pages"] = web_guide_pages
    result = build_web_report_explanation(
        task_id=task_id, agent_run_id=agent_run_id, epoch=epoch, rows=rows
    )
    result["web_logical_pages"] = web_logical_pages
    result["web_ad_pages"] = web_ad_pages
    result["web_guide_pages"] = web_guide_pages
    result["web_identity_diagnostics"] = web_identity_diagnostics
    return result
