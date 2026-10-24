"""H5 新会话恢复阶段的动作许可，不把恢复过程冒充已等价的正式补采。"""

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    EvidenceExitGatePersistenceError,
    PostgresEvidenceExitGate,
    _validate_permit,
)
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_session_permission_sql,
)


class PostgresH5RestorationExitGate(PostgresEvidenceExitGate):
    """仅组合到 H5 恢复 Coordinator；其他动作沿用公共离场门禁。"""

    async def _reserve(self, *, intent: ActionIntent, permit: SessionCommandPermit, **kwargs):
        phase = _restoration_phase(intent)
        if phase is None:
            return await super()._reserve(intent=intent, permit=permit, **kwargs)
        _validate_permit(intent=intent, permit=permit)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            permitted = await connection.fetchval(
                _RESTORATION_PERMISSION_SQL,
                intent.task_id, intent.session_id, intent.source_capture_id,
                permit.lease_owner, permit.lease_token, permit.command_sequence,
                permit.finalization_epoch, intent.parameters.get("restoration_plan_id"),
                intent.parameters.get("evidence_request_id"), intent.deadline,
                intent.parameters.get("recovery_transition_id"),
                intent.action_type.value, intent.semantic_target, phase,
            )
        if not permitted:
            raise EvidenceExitGatePersistenceError("H5 恢复动作的计划、请求、现场或租约已失效")
        # 恢复尚无等价结论，不创建普通业务离场预留，也不伪造六字段补采证明。
        # Coordinator 仍会执行新鲜目标校验、动作落账及 Capture 提交；恢复后的
        # StateEquivalenceGate 与正式补采挂接保持原流程。
        return None


def _restoration_phase(intent: ActionIntent) -> str | None:
    if (
        intent.surface_kind is not SurfaceKind.WEB
        or intent.risk_level is not InteractionRiskLevel.LOW
        or intent.parameters.get("supplemental_evidence_source") != "targeted_recapture"
        or intent.intent_id != intent.idempotency_key
    ):
        return None
    if intent.intent_id.startswith("h5-targeted-recapture-route:"):
        return "route"
    if (
        intent.intent_id.startswith("h5-recapture-entry-normalize:")
        and intent.action_type is InteractionOperation.CLICK
        and (
            (intent.parameters.get("target_mode") == "popup_dismissal"
             and intent.semantic_target == "关闭当前弹窗"
             and intent.parameters.get("popup_dismissal_capability"))
            or (intent.parameters.get("target_mode") == "startup_ad_dismissal"
                and intent.semantic_target == "跳过"
                and intent.parameters.get("startup_ad_capability") == "countdown_close")
        )
        and str(intent.parameters.get("obstruction_signature", "")).startswith("h5-obstruction:")
    ):
        return "clearance"
    return None


_RESTORATION_PERMISSION_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM qc_task AS task
        JOIN qc_surface_session AS session ON session.task_id = task.id
        JOIN qc_capture AS capture
          ON capture.id = session.current_capture_id
         AND capture.task_id = task.id AND capture.surface_session_id = session.session_id
         AND capture.surface_kind = 'web' AND capture.capture_status = 'captured'
        JOIN qc_restoration_attempt AS attempt
          ON attempt.task_id = task.id AND attempt.target_session_id = session.session_id
         AND attempt.finalization_epoch = task.finalization_epoch
         AND attempt.status = 'running' AND attempt.strategy = 'new_session_replay'
        WHERE task.id = $1::uuid AND session.session_id = $2
          AND capture.id = $3::uuid AND session.surface_kind = 'web'
          AND session.status IN ('active', 'draining')
          AND session.finalization_epoch = task.finalization_epoch
          AND session.lease_owner = $4 AND session.lease_token = $5::uuid
          AND session.command_sequence = $6 AND session.finalization_epoch = $7
          AND session.lease_expires_at > now()
          AND session.session_owner = 'h5-snapshot-recapture:' || capture.agent_run_id::text
          AND attempt.plan_id = $8 AND attempt.request_id = $9
          AND attempt.plan_payload->>'plan_id' = $8
          AND (attempt.plan_payload->>'deadline')::timestamptz >= $10
          AND $10::timestamptz > now()
          AND __SESSION_PERMISSION__
          AND ($14 = 'clearance' OR ($14 = 'route' AND EXISTS (
              SELECT 1 FROM qc_page_graph_transition AS edge
              WHERE edge.transition_id = $11
                AND attempt.plan_payload->'normalization_step_ids' ? edge.transition_id
                AND edge.task_id = task.id AND edge.agent_run_id = capture.agent_run_id
                AND edge.surface_session_id = attempt.source_session_id
                AND edge.surface_kind = 'web' AND edge.finalization_epoch = $7
                AND edge.risk_level = 'low' AND edge.status = 'completed'
                AND edge.replay_eligible AND edge.action_kind = $12
                AND edge.semantic_target IS NOT DISTINCT FROM $13
          )))
    )
""".replace("__SESSION_PERMISSION__", h5_snapshot_session_permission_sql(
    attempt_id="attempt.attempt_id",
))
