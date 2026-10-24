"""H5 Completion Gate 专属 SQL 片段，统一正式页面生命周期口径。"""

# 实时发布、恢复对账和 Gate 必须使用同一已判定逻辑状态证明，不能仅按 URL 或页型去重。
H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL = """
lower(task.target_platform) = 'h5' AND EXISTS (
    SELECT 1
    FROM qc_h5_page_type_assessment AS current_assessment
    INNER JOIN qc_h5_page_type_assessment AS previous_assessment
        ON previous_assessment.task_id = current_assessment.task_id
       AND previous_assessment.agent_run_id = current_assessment.agent_run_id
       AND previous_assessment.detection_status = 'recognized'
       AND previous_assessment.page_type = current_assessment.page_type
    INNER JOIN qc_page_capture AS previous_capture
        ON previous_capture.id = previous_assessment.page_capture_id
       AND previous_capture.task_id = current_assessment.task_id
       AND previous_capture.agent_run_id = current_assessment.agent_run_id
    WHERE current_assessment.task_id = task.id
      AND current_assessment.agent_run_id = page_capture.agent_run_id
      AND current_assessment.page_capture_id = page_capture.id
      AND current_assessment.detection_status = 'recognized'
      AND current_assessment.page_type = page_capture.page_type
      AND previous_capture.page_seq < page_capture.page_seq
      AND previous_capture.page_type = page_capture.page_type
      AND previous_capture.page_state IS NOT DISTINCT FROM page_capture.page_state
      AND previous_capture.inspection_status IN ('evaluating', 'completed', 'degraded', 'failed')
      AND COALESCE(previous_capture.inspection_lifecycle_status, '')
          NOT IN ('superseded', 'abandoned', 'evidence_only')
      AND NULLIF(current_assessment.assessment_payload->'detection_result'
          ->'logical_surface'->>'identity_fingerprint', '') IS NOT NULL
      AND NULLIF(current_assessment.assessment_payload->'detection_result'
          ->'logical_surface'->>'state_fingerprint', '') IS NOT NULL
      AND previous_assessment.assessment_payload->'detection_result'
          ->'logical_surface'->>'identity_fingerprint'
          = current_assessment.assessment_payload->'detection_result'
              ->'logical_surface'->>'identity_fingerprint'
      AND previous_assessment.assessment_payload->'detection_result'
          ->'logical_surface'->>'state_fingerprint'
          = current_assessment.assessment_payload->'detection_result'
              ->'logical_surface'->>'state_fingerprint'
)
""".strip()

def _h5_inventory_cte(*, name: str, deduplicate: bool) -> str:
    # 去重只决定是否新增发布，不能抹掉既有发布记录的 Manifest 审计依据。
    duplicate_filter = (
        f"AND NOT ({H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL})" if deduplicate else ""
    )
    return f"""
{name} AS (
    SELECT
        inventory.after_capture_id,
        inventory.agent_run_id,
        inventory.published_inspection_count
    FROM target_task AS task
    INNER JOIN qc_h5_evidence_inventory_item AS inventory
        ON inventory.task_id = task.id
       AND inventory.status = 'pass'
       AND inventory.after_capture_id IS NOT NULL
    INNER JOIN qc_action_execution AS action
        ON action.task_id = task.id
       AND action.session_id = inventory.session_id
       AND action.action_execution_id = inventory.action_execution_id
    INNER JOIN qc_surface_session AS surface_session
        ON surface_session.task_id = task.id
       AND surface_session.session_id = action.session_id
       AND surface_session.surface_kind = 'web'
       AND action.finalization_epoch = surface_session.finalization_epoch
       AND surface_session.finalization_epoch = task.finalization_epoch
    INNER JOIN qc_capture AS capture
        ON capture.id = inventory.after_capture_id
       AND capture.task_id = task.id
       AND capture.agent_run_id = inventory.agent_run_id
       AND capture.capture_role = 'after_action'
    INNER JOIN qc_page_capture AS page_capture
        ON page_capture.id = capture.id
       AND page_capture.task_id = task.id
       AND page_capture.page_type = inventory.page_type
    INNER JOIN qc_evidence_manifest AS before_manifest
        ON before_manifest.manifest_id = inventory.before_manifest_id
       AND before_manifest.task_id = task.id
       AND before_manifest.capture_id = inventory.before_capture_id
       AND before_manifest.manifest_hash = inventory.before_manifest_hash
       AND before_manifest.quality_status = 'pass'
    INNER JOIN qc_evidence_manifest AS after_manifest
        ON after_manifest.manifest_id = inventory.after_manifest_id
       AND after_manifest.task_id = task.id
       AND after_manifest.capture_id = inventory.after_capture_id
       AND after_manifest.manifest_hash = inventory.after_manifest_hash
       AND after_manifest.quality_status = 'pass'
    WHERE task.target_platform IN ('h5', 'web')
      AND inventory.authority_source = 'pass_manifest'
      AND task.task_config->>'execution_mode' = 'full_inspection'
      AND task.task_config->>'h5_inspection_notification_mode' = 'enforced'
      AND (
            task.target_platform <> 'h5'
            OR COALESCE(
                action.intent_payload->'parameters'->>'supplemental_evidence_source',
                ''
            ) <> 'targeted_recapture'
          )
      AND (
            task.finalization_agent_run_id = inventory.agent_run_id
            OR task.finalization_agent_run_id IS NULL
          )
      AND COALESCE(
            page_capture.inspection_lifecycle_status,
            capture.metadata->>'inspection_lifecycle_status',
            ''
          ) NOT IN ('superseded', 'abandoned')
      {duplicate_filter}
      AND EXISTS (
          SELECT 1
          FROM qc_h5_page_type_assessment AS assessment
          WHERE assessment.task_id = task.id
            AND assessment.agent_run_id = inventory.agent_run_id
            AND assessment.page_capture_id = inventory.after_capture_id
            AND assessment.detection_status = 'recognized'
            AND assessment.page_type = inventory.page_type
      )
      AND NOT EXISTS (
          SELECT 1
          FROM qc_evidence_manifest AS newer_before
          WHERE newer_before.capture_id = before_manifest.capture_id
            AND newer_before.manifest_version > before_manifest.manifest_version
      )
      AND NOT EXISTS (
          SELECT 1
          FROM qc_evidence_manifest AS newer_after
          WHERE newer_after.capture_id = after_manifest.capture_id
            AND newer_after.manifest_version > after_manifest.manifest_version
      )
)
""".strip()


QUALIFIED_H5_INVENTORY_CTE_SQL = _h5_inventory_cte(
    name="qualified_h5_inventory", deduplicate=True,
)
AUDITABLE_H5_INVENTORY_CTE_SQL = _h5_inventory_cte(
    name="auditable_h5_inventory", deduplicate=False,
)
