"""认证历史分类原材料与本次观察的冻结逻辑页关系。"""

from hashlib import sha256

from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
from services.api.app.infrastructure.db.web_capture_identity import (
    resolve_persisted_capture_identity,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


async def read_reused_classification(
    connection,
    *,
    task_id,
    run_id,
    session_id,
    finalization_epoch,
    capture_id,
    reused_capture_id,
    reused_logical_page_id,
):
    if capture_id == reused_capture_id:
        return None
    row = await connection.fetchrow(
        """SELECT fresh_page.url, target.identity_policy_version,
                  original_page.url AS original_url,
                  original_page.metadata->>'logical_page_id' AS page_logical_id,
                  original.metadata->>'logical_page_id' AS common_logical_id,
                  original_page.metadata->>'identity_policy_version' AS page_identity_version,
                  original.metadata->>'identity_policy_version' AS common_identity_version,
                  target.logical_page_id AS target_logical_id,
                  target.identity_policy_version AS target_identity_version,
                  assessment.assessment_payload->>'final_page_type' AS page_type
           FROM qc_task task
           JOIN qc_surface_session session ON session.task_id=task.id
             AND session.session_id=$3 AND session.session_owner='web-v2-workflow'
             AND session.surface_kind='web' AND session.finalization_epoch=$4
           JOIN qc_capture fresh ON fresh.task_id=task.id AND fresh.id=$5::uuid
             AND fresh.agent_run_id=$2::uuid AND fresh.surface_session_id=session.session_id
             AND fresh.surface_kind='web' AND fresh.capture_status='captured'
             AND fresh.metadata->>'finalization_epoch'=$4::text
           JOIN qc_page_capture fresh_page ON fresh_page.id=fresh.id
             AND fresh_page.task_id=task.id AND fresh_page.agent_run_id=$2::uuid
             AND fresh_page.surface_session_id=session.session_id
             AND fresh_page.capture_status='captured'
             AND fresh_page.metadata->>'finalization_epoch'=$4::text
           JOIN qc_web_logical_target target ON target.task_id=task.id
             AND target.agent_run_id=$2::uuid AND target.session_id=session.session_id
             AND target.finalization_epoch=$4 AND target.state='materialized'
             AND target.logical_page_id=$7 AND target.assessment_capture_id=$6::uuid
           JOIN qc_capture original ON original.id=target.assessment_capture_id
             AND original.task_id=task.id AND original.agent_run_id=$2::uuid
             AND original.surface_session_id=session.session_id AND original.surface_kind='web'
             AND original.capture_status='captured'
             AND original.metadata->>'finalization_epoch'=$4::text
             AND original.metadata->>'logical_page_id'=target.logical_page_id
           JOIN qc_page_capture original_page ON original_page.id=original.id
             AND original_page.task_id=task.id AND original_page.agent_run_id=$2::uuid
             AND original_page.surface_session_id=session.session_id
             AND original_page.capture_status='captured'
             AND original_page.metadata->>'finalization_epoch'=$4::text
             AND original_page.metadata->>'logical_page_id'=target.logical_page_id
           JOIN qc_h5_page_type_assessment assessment ON assessment.page_capture_id=original.id
             AND assessment.task_id=task.id AND assessment.agent_run_id=$2::uuid
             AND assessment.session_id=session.session_id
             AND assessment.schema_version IN ('web-page-type-assessment.v1',
                 'web-page-type-assessment.v2','web-page-type-assessment.v3','web-page-type-assessment.v4')
             AND original_page.metadata #>> '{web_page_type_assessment,version}'
                 =assessment.schema_version
             AND original_page.metadata #>> '{web_page_type_assessment,assessment_id}'
                 =assessment.assessment_id
             AND assessment.assessment_payload->>'schema_version'=assessment.schema_version
             AND assessment.assessment_payload->'page_capture_ids'
                 @> jsonb_build_array(original.id::text)
             AND original_page.page_type IS NOT DISTINCT FROM
                 assessment.assessment_payload->>'final_page_type'
             AND assessment.page_type=assessment.assessment_payload->>'page_type'
             AND assessment.detection_status=assessment.assessment_payload->>'detection_status'
             AND (assessment.detection_status='needs_review'
                  OR original_page.page_type=assessment.page_type)
           WHERE task.id=$1::uuid AND task.target_platform='web' AND task.finalization_epoch=$4
             AND COALESCE(original_page.inspection_lifecycle_status,
                          original.metadata->>'inspection_lifecycle_status','')
                 NOT IN ('superseded','abandoned')
           LIMIT 1""",
        task_id,
        run_id,
        session_id,
        finalization_epoch,
        capture_id,
        reused_capture_id,
        reused_logical_page_id,
    )
    if row is None:
        return None
    snapshot = await read_web_policy_on_connection(connection, task_id, run_id)
    if snapshot is None:
        return None
    policy = LegacyWebRunAuditAdapter.load(snapshot).identity_policy
    if policy.version != row["identity_policy_version"]:
        return None
    identity = resolve_persisted_capture_identity(
        {
            **dict(row),
            "url": row["original_url"],
            "assessment_present": True,
            "target_material_present": True,
        },
        policy,
    )
    if identity.get("logical_page_identity_status") != "verified":
        return None
    try:
        logical_id = policy.logical_id(row["url"])
    except ValueError:
        return None
    alias = await connection.fetchval(
        "SELECT logical_page_id FROM qc_web_logical_target_alias WHERE task_id=$1::uuid "
        "AND agent_run_id=$2::uuid AND alias_logical_page_id=$3 "
        "AND evidence_kind='natural_redirect'",
        task_id,
        run_id,
        logical_id,
    )
    if (alias or logical_id) != reused_logical_page_id:
        return None
    return {
        "page_type": row["page_type"],
        "reused_source_url_sha256": sha256(row["original_url"].encode("utf-8")).hexdigest(),
        "reused_observed_url_sha256": sha256(row["url"].encode("utf-8")).hexdigest(),
    }


def web_classification_reuse_cte(run_id_sql: str) -> str:
    """只替换经认证的观察引用；材料完整性仍由各正式库存独立核验。"""
    if run_id_sql not in {"$2::uuid", "task.finalization_agent_run_id"}:
        raise ValueError("复用库存只接受已定义的 Run 来源")
    return f"""
web_classification_reuse AS (
    SELECT DISTINCT task.id AS task_id, target.agent_run_id,
           fresh.id AS observed_capture_id, original.id AS assessment_capture_id
    FROM target_task task
    JOIN qc_web_menu_ledger_entry entry ON entry.agent_run_id={run_id_sql}
      AND entry.status='logical_target_reused'
    JOIN qc_web_logical_target target ON target.task_id=task.id
      AND target.agent_run_id=entry.agent_run_id AND target.state='materialized'
      AND target.finalization_epoch=task.finalization_epoch
      AND target.logical_page_id=entry.diagnostic->>'reused_logical_page_id'
      AND target.assessment_capture_id::text=entry.diagnostic->>'reused_capture_id'
    JOIN qc_web_run_policy_snapshot policy ON policy.task_id=task.id
      AND policy.agent_run_id=target.agent_run_id
      AND policy.payload->>'identity_policy_version'=target.identity_policy_version
      AND target.identity_policy_version='web-url-identity.v2'
    JOIN qc_surface_session session ON session.task_id=task.id
      AND session.session_id=target.session_id
      AND session.session_id='web:' || target.agent_run_id::text
      AND session.surface_kind='web' AND session.session_owner='web-v2-workflow'
      AND session.finalization_epoch=task.finalization_epoch
    JOIN qc_capture original ON original.id=target.assessment_capture_id
      AND original.task_id=task.id AND original.agent_run_id=target.agent_run_id
      AND original.surface_session_id=session.session_id AND original.surface_kind='web'
      AND original.capture_status='captured' AND original.capture_role='after_action'
      AND original.metadata->>'finalization_epoch'=task.finalization_epoch::text
      AND original.metadata->>'logical_page_id'=target.logical_page_id
      AND COALESCE(original.metadata->>'identity_policy_version',target.identity_policy_version)
          =target.identity_policy_version
    JOIN qc_page_capture original_page ON original_page.id=original.id
      AND original_page.task_id=task.id AND original_page.agent_run_id=target.agent_run_id
      AND original_page.surface_session_id=session.session_id
      AND original_page.capture_status='captured'
      AND original_page.metadata->>'finalization_epoch'=task.finalization_epoch::text
      AND original_page.metadata->>'logical_page_id'=target.logical_page_id
      AND COALESCE(original_page.metadata->>'identity_policy_version',
                   target.identity_policy_version)
          =target.identity_policy_version
    JOIN qc_h5_page_type_assessment assessment ON assessment.page_capture_id=original.id
      AND assessment.task_id=task.id AND assessment.agent_run_id=target.agent_run_id
      AND assessment.session_id=session.session_id
      AND assessment.schema_version IN ('web-page-type-assessment.v1','web-page-type-assessment.v2',
          'web-page-type-assessment.v3','web-page-type-assessment.v4')
      AND original_page.metadata #>> '{{web_page_type_assessment,version}}'
          =assessment.schema_version
      AND original_page.metadata #>> '{{web_page_type_assessment,assessment_id}}'
          =assessment.assessment_id
      AND assessment.assessment_payload->>'schema_version'=assessment.schema_version
      AND assessment.assessment_payload->'page_capture_ids' @> jsonb_build_array(original.id::text)
      AND original_page.page_type IS NOT DISTINCT FROM
          assessment.assessment_payload->>'final_page_type'
      AND assessment.page_type=assessment.assessment_payload->>'page_type'
      AND assessment.detection_status=assessment.assessment_payload->>'detection_status'
      AND (assessment.detection_status='needs_review'
           OR original_page.page_type=assessment.page_type)
    JOIN qc_capture fresh ON fresh.task_id=task.id AND fresh.agent_run_id=target.agent_run_id
      AND fresh.id=CASE entry.diagnostic->>'reused_target_stage'
          WHEN 'menu' THEN entry.menu_ready_capture_id
          WHEN 'detail_after_action' THEN entry.detail_ready_capture_id END
      AND fresh.surface_session_id=session.session_id AND fresh.surface_kind='web'
      AND fresh.capture_status='captured' AND fresh.capture_role='after_action'
      AND fresh.metadata->>'finalization_epoch'=task.finalization_epoch::text
      AND fresh.id<>original.id
    JOIN qc_page_capture fresh_page ON fresh_page.id=fresh.id AND fresh_page.task_id=task.id
      AND fresh_page.agent_run_id=target.agent_run_id
      AND fresh_page.surface_session_id=session.session_id AND fresh_page.capture_status='captured'
      AND fresh_page.metadata->>'finalization_epoch'=task.finalization_epoch::text
    WHERE task.target_platform='web'
      AND COALESCE(original_page.inspection_lifecycle_status,
                   original.metadata->>'inspection_lifecycle_status','')
          NOT IN ('superseded','abandoned')
      AND (
          EXISTS (
              SELECT 1 FROM qc_surface_phase_run phase
              WHERE phase.task_id=task.id AND phase.session_id=session.session_id
                AND phase.source_capture_id=fresh.id AND phase.phase_key='web_stage_classification'
                AND phase.phase_protocol_version='web-run-stage-evidence.v1'
                AND phase.status='completed' AND phase.finalization_epoch=task.finalization_epoch
                AND phase.context_payload->>'capture_id'=fresh.id::text
                AND phase.context_payload->>'reused_capture_id'=original.id::text
                AND phase.context_payload->>'reused_logical_page_id'=target.logical_page_id
                AND phase.context_payload->>'reused_source_url_sha256'
                    =encode(sha256(convert_to(original_page.url,'UTF8')),'hex')
                AND phase.context_payload->>'reused_observed_url_sha256'
                    =encode(sha256(convert_to(fresh_page.url,'UTF8')),'hex')
          )
          OR (
              fresh_page.url=original_page.url
              AND target.logical_page_id='web-logical:' ||
                  encode(sha256(convert_to(original_page.url,'UTF8')),'hex')
              AND NOT EXISTS (
                  SELECT 1 FROM qc_surface_phase_run recorded
                  WHERE recorded.task_id=task.id AND recorded.session_id=session.session_id
                    AND recorded.source_capture_id=fresh.id
                    AND recorded.phase_key='web_stage_classification'
                    AND recorded.context_payload ? 'reused_capture_id'
              )
          )
      )
)
"""
