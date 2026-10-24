"""异常收口只检查真实活动工作，缺少候选或覆盖不能冒充仍在执行。"""

from services.api.app.infrastructure.db.h5_browser_stop_receipt import unconfirmed_h5_stop_sql

_STALLED_FINALIZATION_READY_BODY = f"""
    SELECT EXISTS (
        SELECT 1 FROM qc_agent_run AS owner_run
        WHERE owner_run.id=$2::uuid AND owner_run.task_id=$1::uuid
          AND owner_run.agent_type='main'
          AND owner_run.status IN ('done','failed','aborted')
          AND owner_run.finished_at <= now()-($3 * interval '1 second')
          AND COALESCE(owner_run.output_context->>'cancel_requested','false') <> 'true'
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_agent_run WHERE task_id=$1::uuid AND status IN ('pending','running')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_surface_session AS session WHERE task_id=$1::uuid
          AND (status NOT IN ('closed','lost','failed') OR lease_owner IS NOT NULL
               OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL
               OR {unconfirmed_h5_stop_sql()})
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_surface_phase_run WHERE task_id=$1::uuid AND status='running'
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_h5_session_journey_guard WHERE task_id=$1::text
          AND status IN ('active','quarantined')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_device_lease WHERE task_id=$1::uuid
          AND state IN ('claimed','active','releasing','recovering')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_app_run_execution_lease WHERE task_id=$1::uuid
          AND lease_expires_at > now()
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_v2_work_item WHERE task_id=$1::uuid
          AND status NOT IN ('succeeded','bounded','budget_exhausted','blocked',
                             'no_progress','failed','cancelled','superseded')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_inspection_work_item WHERE task_id=$1::uuid
          AND status NOT IN ('succeeded','partial','failed','cancelled','superseded')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid
          AND status NOT IN ('succeeded','evidence_insufficient','failed','cancelled','superseded')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_aggregate_inspection_candidate WHERE task_id=$1::uuid
          AND status NOT IN ('completed','not_applicable','needs_review',
                             'evidence_insufficient','failed','cancelled','superseded')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_evidence_request WHERE task_id=$1::uuid
          AND status IN ('pending','routed','running')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_h5_inspection_publication WHERE task_id=$1::uuid
          AND status IN ('pending','leased','retry_wait')
    )
    AND NOT EXISTS (
        SELECT 1 FROM qc_app_inspection_publication WHERE task_id=$1::uuid
          AND status IN ('pending','leased','retry_wait')
    )
"""

# App 庫未必建了 Web 階段表。把這段寫進同一條 SQL 會在規劃時直接報
# UndefinedTable，異常收口永遠失敗，任務停在 running。
_WEB_CHECKPOINT_NOT_ACTIVE = """
    AND NOT EXISTS (
        SELECT 1 FROM qc_web_run_stage_checkpoint AS checkpoint
        JOIN qc_agent_run AS run ON run.id=checkpoint.run_id
        WHERE run.task_id=$1::uuid
          AND (checkpoint.stage_status NOT IN ('succeeded','gapped','cancelled')
               OR checkpoint.lease_expires_at > now())
    )
"""

STALLED_FINALIZATION_READY_SQL = (
    _STALLED_FINALIZATION_READY_BODY + _WEB_CHECKPOINT_NOT_ACTIVE
)
STALLED_FINALIZATION_READY_SQL_WITHOUT_WEB_CHECKPOINT = _STALLED_FINALIZATION_READY_BODY
