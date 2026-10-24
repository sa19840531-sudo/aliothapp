"""H5 Probe 动作账本的证据角色适配。"""

from __future__ import annotations

from services.api.app.application.action_executions import ActionExecutionSnapshot
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    ActionExecutionStatus,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
    _json_dumps,
)


class PostgresH5ProbeActionExecutionRepository(PostgresActionExecutionRepository):
    """只允许带定向补采血缘的 H5 ``probe_before`` 推进为 executing。"""

    async def mark_executing(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        source_capture_id: str,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'executing',
                    source_capture_id = capture.id,
                    started_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.status = 'planned'
                  AND action.row_version = $2
                  AND action.session_id LIKE 'h5:%'
                  AND action.intent_payload #>> '{capture_profile,before,role}'
                        = 'probe_before'
                  AND action.intent_payload
                        #>> '{parameters,supplemental_evidence_source}'
                        = 'targeted_recapture'
                  AND NULLIF(
                        action.intent_payload #>> '{parameters,evidence_request_id}',
                        ''
                      ) IS NOT NULL
                  AND capture.id = $3::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.surface_kind = 'web'
                  AND capture.capture_role = 'probe_before'
                  AND capture.capture_status = 'captured'
                  AND capture.parent_capture_id
                        IS NOT DISTINCT FROM action.requested_source_capture_id
                  AND capture.session_command_sequence = action.command_sequence
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = 'web'
                      AND session.lease_token = $4::uuid
                      AND session.lease_owner = $5
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                action_execution_id,
                expected_row_version,
                source_capture_id,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="H5 Probe 动作前证据、补采血缘、版本或 Session 租约已变化",
        )

    async def complete_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        if (
            interaction.status is not InteractionResultStatus.SUCCEEDED
            or interaction.result_capture_id is None
        ):
            raise ValueError("完成证据必须提供 succeeded InteractionResult")
        if permit.surface_kind is not SurfaceKind.WEB:
            raise ValueError("H5 Probe 只允许 Web Surface")
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    status = 'completed',
                    result_capture_id = capture.id,
                    result_payload = $4::jsonb,
                    evidence_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.status = 'evidence_pending'
                  AND action.row_version = $2
                  AND action.task_id = $5::uuid
                  AND action.session_id = $6
                  AND action.session_id LIKE 'h5:%'
                  AND action.intent_id = $7
                  AND action.source_capture_id = $8::uuid
                  AND action.intent_payload #>> '{capture_profile,after,role}'
                        = 'probe_result_replay'
                  AND action.intent_payload
                        #>> '{parameters,supplemental_evidence_source}'
                        = 'targeted_recapture'
                  AND NULLIF(
                        action.intent_payload #>> '{parameters,evidence_request_id}',
                        ''
                      ) IS NOT NULL
                  AND capture.id = $3::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.surface_kind = 'web'
                  AND capture.capture_role = 'probe_result_replay'
                  AND capture.capture_status = 'captured'
                  AND capture.parent_capture_id = action.source_capture_id
                  AND capture.session_command_sequence = action.command_sequence
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = 'web'
                      AND session.lease_token = $9::uuid
                      AND session.lease_owner = $10
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                interaction.action_execution_id,
                expected_row_version,
                interaction.result_capture_id,
                _json_dumps(interaction.to_dict()),
                interaction.task_id,
                interaction.session_id,
                interaction.intent_id,
                interaction.source_capture_id,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="H5 Probe 结果证据、补采血缘、版本或 Session 租约已变化",
        )

    async def attach_terminal_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        """把 H5 Probe 的动作后证据挂到既有失败终态。"""
        status_map = {
            InteractionResultStatus.FAILED: ActionExecutionStatus.FAILED,
            InteractionResultStatus.BLOCKED: ActionExecutionStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT: ActionExecutionStatus.TIMED_OUT,
        }
        expected_status = status_map.get(interaction.status)
        if expected_status is None or interaction.result_capture_id is None:
            raise ValueError("终态证据必须携带失败、阻断或超时结果及 Capture")
        if permit.surface_kind is not SurfaceKind.WEB:
            raise ValueError("H5 Probe 只允许 Web Surface")
        return await self._transition(
            query="""
                UPDATE qc_action_execution AS action
                SET
                    result_capture_id = capture.id,
                    result_payload = $4::jsonb,
                    evidence_finished_at = now(),
                    row_version = action.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE action.action_execution_id = $1
                  AND action.row_version = $2
                  AND action.status = $3
                  AND action.task_id = $5::uuid
                  AND action.session_id = $6
                  AND action.session_id LIKE 'h5:%'
                  AND action.intent_id = $7
                  AND action.source_capture_id = $8::uuid
                  AND action.result_capture_id IS NULL
                  AND action.intent_payload #>> '{capture_profile,after,role}'
                        = 'probe_result_replay'
                  AND action.intent_payload
                        #>> '{parameters,supplemental_evidence_source}'
                        = 'targeted_recapture'
                  AND NULLIF(
                        action.intent_payload #>> '{parameters,evidence_request_id}',
                        ''
                      ) IS NOT NULL
                  AND capture.id = $9::uuid
                  AND capture.task_id = action.task_id
                  AND capture.surface_session_id = action.session_id
                  AND capture.action_execution_id = action.action_execution_id
                  AND capture.surface_kind = 'web'
                  AND capture.capture_role = 'probe_result_replay'
                  AND capture.capture_status = 'captured'
                  AND capture.parent_capture_id = action.source_capture_id
                  AND capture.session_command_sequence = action.command_sequence
                  AND EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id = action.task_id
                      AND session.session_id = action.session_id
                      AND session.surface_kind = 'web'
                      AND session.lease_token = $10::uuid
                      AND session.lease_owner = $11
                      AND session.lease_expires_at > now()
                      AND session.command_sequence = action.command_sequence
                      AND session.finalization_epoch = action.finalization_epoch
                  )
                RETURNING action.*
            """,
            args=(
                interaction.action_execution_id,
                expected_row_version,
                expected_status.value,
                _json_dumps(interaction.to_dict()),
                interaction.task_id,
                interaction.session_id,
                interaction.intent_id,
                interaction.source_capture_id,
                interaction.result_capture_id,
                permit.lease_token,
                permit.lease_owner,
            ),
            error="H5 Probe 终态证据、补采血缘、版本或 Session 租约已变化",
        )


__all__ = ["PostgresH5ProbeActionExecutionRepository"]
