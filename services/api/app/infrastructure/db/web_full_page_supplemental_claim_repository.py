"""复用 Surface 阶段唯一键，永久消费 Web ready Capture 的一次全页补图。"""

from __future__ import annotations

import json
from hashlib import sha256

from services.api.app.application.web_full_page_supplemental_capture import (
    WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION,
    WebFullPageSupplementalResult,
)
from services.api.app.domain.exploration.surface_phase import (
    build_surface_phase_payload_hash,
    build_surface_phase_run_id,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageStatus
from services.api.app.infrastructure.db.pool import get_postgres_pool

_FORMAL_READY_CAPTURE_ROLES = ("after_action", "primary")
_AUXILIARY_PHASE_PROTOCOL_VERSION = "web-auxiliary-navigation.v1"
_AUXILIARY_PHASE_KEYS = frozenset({"web_search_discovery", "web_activity_navigation"})

_SUPPLEMENTAL_CHILD_SCOPE_SQL = """
          AND child.task_id=phase.task_id
          AND child.agent_run_id=(phase.context_payload->>'agent_run_id')::uuid
          AND child.surface_session_id=phase.session_id
          AND child.surface_kind='web'
          AND child.parent_capture_id=phase.source_capture_id
          AND child.capture_role='supplemental'
          AND child.capture_status='captured'
          AND child.session_command_sequence=phase.command_sequence
          AND jsonb_typeof(child.metadata->'finalization_epoch')='number'
          AND child.metadata->>'finalization_epoch'=phase.finalization_epoch::text
          AND child.metadata->>'capture_purpose'='web_full_page_supplemental'
          AND page.task_id=child.task_id
          AND page.agent_run_id=child.agent_run_id
          AND page.parent_page_capture_id=phase.source_capture_id
          AND page.capture_role='supplemental'
          AND page.capture_status='captured'
          AND page.session_command_sequence=phase.command_sequence
          AND jsonb_typeof(page.metadata->'finalization_epoch')='number'
          AND page.metadata->>'finalization_epoch'=phase.finalization_epoch::text
          AND page.metadata->>'capture_purpose'='web_full_page_supplemental'
"""

_SUPPLEMENTAL_SOURCE_JOINS_SQL = """
        JOIN qc_capture source ON source.id=phase.source_capture_id
          AND source.task_id=phase.task_id
          AND source.agent_run_id=(phase.context_payload->>'agent_run_id')::uuid
          AND source.surface_session_id=phase.session_id
          AND source.surface_kind='web'
          AND source.capture_status='captured'
          AND jsonb_typeof(source.metadata->'finalization_epoch')='number'
          AND source.metadata->>'finalization_epoch'=phase.finalization_epoch::text
        JOIN qc_page_capture source_page ON source_page.id=source.id
          AND source_page.task_id=source.task_id
          AND source_page.agent_run_id=source.agent_run_id
          AND source_page.surface_session_id=source.surface_session_id
          AND source_page.capture_status='captured'
          AND source_page.session_command_sequence=source.session_command_sequence
          AND jsonb_typeof(source_page.metadata->'finalization_epoch')='number'
          AND source_page.metadata->>'finalization_epoch'=phase.finalization_epoch::text
"""

_SUPPLEMENTAL_FINISH_SQL = f"""
UPDATE qc_surface_phase_run AS phase
SET status=$2,terminal_reason_code=$3,
    evidence_artifact_ids=$4::jsonb,finished_at=now(),updated_at=now(),
    row_version=row_version+1
WHERE phase.phase_run_id=$1
  AND phase.phase_key='web_full_page_supplemental'
  AND phase.phase_protocol_version=$5
  AND phase.status='running'
  AND (
    ($2='failed' AND jsonb_array_length($4::jsonb)=0)
    OR
    ($2='completed' AND (
      (jsonb_array_length($4::jsonb)=1 AND EXISTS (
        SELECT 1 FROM qc_evidence_artifact artifact
        JOIN qc_capture child ON child.id=artifact.page_capture_id
        JOIN qc_page_capture page ON page.id=child.id
        {_SUPPLEMENTAL_SOURCE_JOINS_SQL}
        WHERE artifact.id::text=($4::jsonb)->>0
          AND artifact.task_id=phase.task_id
          AND artifact.artifact_type='screenshot'
          AND split_part(lower(artifact.content_type),';',1)='image/png'
          AND artifact.metadata->>'purpose'='web_direct_full_page'
          {_SUPPLEMENTAL_CHILD_SCOPE_SQL}
          AND NOT EXISTS (SELECT 1 FROM qc_evidence_artifact extra
            WHERE extra.page_capture_id=child.id
              AND extra.artifact_type='page_coverage')
      ))
      OR
      (jsonb_array_length($4::jsonb)>=2 AND EXISTS (
        SELECT 1 FROM qc_evidence_artifact manifest
        JOIN qc_capture child ON child.id=manifest.page_capture_id
        JOIN qc_page_capture page ON page.id=child.id
        {_SUPPLEMENTAL_SOURCE_JOINS_SQL}
        WHERE manifest.id::text=($4::jsonb)->>-1
          AND manifest.task_id=phase.task_id
          AND manifest.artifact_type='page_coverage'
          AND split_part(lower(manifest.content_type),';',1)='application/json'
          AND manifest.metadata->>'protocol_version' IN (
              'web-full-page-segment-manifest.v1',
              'web-scrolled-viewport-manifest.v1'
          )
          AND manifest.metadata->>'associated_page_capture_id'=child.id::text
          AND jsonb_typeof(manifest.metadata->'associated_screenshot_artifact_ids')='array'
          AND (
              (
                manifest.metadata->>'protocol_version'='web-full-page-segment-manifest.v1'
                AND jsonb_typeof(manifest.metadata->'segment_count')='number'
                AND jsonb_array_length(
                    manifest.metadata->'associated_screenshot_artifact_ids'
                )=(manifest.metadata->>'segment_count')::integer
              )
              OR
              (
                manifest.metadata->>'protocol_version'='web-scrolled-viewport-manifest.v1'
                AND manifest.metadata->>'capture_mode'='scroll_offset_viewport'
                AND jsonb_array_length(
                    manifest.metadata->'associated_screenshot_artifact_ids'
                )=1
              )
          )
          AND $4::jsonb=(manifest.metadata->'associated_screenshot_artifact_ids')
              || jsonb_build_array(manifest.id::text)
          {_SUPPLEMENTAL_CHILD_SCOPE_SQL}
          AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(
              manifest.metadata->'associated_screenshot_artifact_ids'
            ) WITH ORDINALITY reference(artifact_id,segment_number)
            LEFT JOIN qc_evidence_artifact screenshot
              ON screenshot.id::text=reference.artifact_id
             AND screenshot.task_id=phase.task_id
             AND screenshot.page_capture_id=child.id
             AND screenshot.artifact_type='screenshot'
             AND split_part(lower(screenshot.content_type),';',1)='image/png'
            WHERE screenshot.id IS NULL
          )
      ))
    ))
  )
RETURNING phase.phase_run_id
"""

_SUPPLEMENTAL_READ_TERMINAL_SQL = f"""
SELECT phase.status,phase.terminal_reason_code,
       valid_child.supplemental_capture_id,
       valid_child.screenshot_artifact_ids,
       valid_child.manifest_artifact_id
FROM qc_surface_phase_run phase
LEFT JOIN LATERAL (
    WITH segmented_candidates AS (
        SELECT child.id AS supplemental_capture_id,
               manifest.metadata->'associated_screenshot_artifact_ids'
                 AS screenshot_artifact_ids,
               manifest.id AS manifest_artifact_id
        FROM qc_evidence_artifact manifest
        JOIN qc_capture child ON child.id=manifest.page_capture_id
        JOIN qc_page_capture page ON page.id=child.id
        {_SUPPLEMENTAL_SOURCE_JOINS_SQL}
        WHERE manifest.task_id=phase.task_id
          AND manifest.artifact_type='page_coverage'
          AND split_part(lower(manifest.content_type),';',1)='application/json'
          AND manifest.metadata->>'protocol_version' IN (
              'web-full-page-segment-manifest.v1',
              'web-scrolled-viewport-manifest.v1'
          )
          AND manifest.metadata->>'associated_page_capture_id'=child.id::text
          AND jsonb_typeof(manifest.metadata->'associated_screenshot_artifact_ids')='array'
          AND (
              (
                manifest.metadata->>'protocol_version'='web-full-page-segment-manifest.v1'
                AND jsonb_typeof(manifest.metadata->'segment_count')='number'
                AND jsonb_array_length(
                    manifest.metadata->'associated_screenshot_artifact_ids'
                )=(manifest.metadata->>'segment_count')::integer
              )
              OR
              (
                manifest.metadata->>'protocol_version'='web-scrolled-viewport-manifest.v1'
                AND manifest.metadata->>'capture_mode'='scroll_offset_viewport'
                AND jsonb_array_length(
                    manifest.metadata->'associated_screenshot_artifact_ids'
                )=1
              )
          )
          AND jsonb_array_length(manifest.metadata->'associated_screenshot_artifact_ids')>0
          {_SUPPLEMENTAL_CHILD_SCOPE_SQL}
          AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(
              manifest.metadata->'associated_screenshot_artifact_ids'
            ) WITH ORDINALITY reference(artifact_id,segment_number)
            LEFT JOIN qc_evidence_artifact screenshot
              ON screenshot.id::text=reference.artifact_id
             AND screenshot.task_id=phase.task_id
             AND screenshot.page_capture_id=child.id
             AND screenshot.artifact_type='screenshot'
             AND split_part(lower(screenshot.content_type),';',1)='image/png'
            WHERE screenshot.id IS NULL
          )
    ), legacy_candidates AS (
        SELECT child.id AS supplemental_capture_id,
               jsonb_build_array(artifact.id::text) AS screenshot_artifact_ids,
               NULL::uuid AS manifest_artifact_id
        FROM qc_evidence_artifact artifact
        JOIN qc_capture child ON child.id=artifact.page_capture_id
        JOIN qc_page_capture page ON page.id=child.id
        {_SUPPLEMENTAL_SOURCE_JOINS_SQL}
        WHERE artifact.task_id=phase.task_id
          AND artifact.artifact_type='screenshot'
          AND split_part(lower(artifact.content_type),';',1)='image/png'
          AND (artifact.metadata->>'purpose'='web_direct_full_page'
               OR (phase.status='completed' AND artifact.metadata->>'purpose' IS NULL))
          {_SUPPLEMENTAL_CHILD_SCOPE_SQL}
          AND NOT EXISTS (SELECT 1 FROM qc_evidence_artifact extra
            WHERE extra.page_capture_id=child.id
              AND extra.artifact_type='page_coverage')
          AND 1=(SELECT count(*) FROM qc_evidence_artifact only_screenshot
            WHERE only_screenshot.page_capture_id=child.id
              AND only_screenshot.artifact_type='screenshot'
              AND split_part(lower(only_screenshot.content_type),';',1)='image/png')
    ), valid_candidates AS (
        SELECT * FROM segmented_candidates
        UNION ALL
        SELECT * FROM legacy_candidates
    )
    SELECT supplemental_capture_id::text,
           screenshot_artifact_ids,
           manifest_artifact_id::text
    FROM valid_candidates
    WHERE (SELECT count(*) FROM valid_candidates)=1
) valid_child ON true
WHERE phase.phase_run_id=$1
  AND phase.task_id=$2::uuid
  AND phase.session_id=$4
  AND phase.source_capture_id=$5::uuid
  AND phase.surface_kind='web'
  AND phase.phase_key='web_full_page_supplemental'
  AND phase.phase_protocol_version=$7
  AND phase.context_payload->>'agent_run_id'=$3::text
  AND phase.context_payload->>'source_capture_id'=$5::text
  AND phase.context_payload->>'source_url_sha256'=$6
  AND (
    phase.status='running'
    OR (phase.status='failed' AND jsonb_array_length(phase.evidence_artifact_ids)=0)
    OR (
      phase.status='completed'
      AND phase.evidence_artifact_ids=valid_child.screenshot_artifact_ids
        || CASE WHEN valid_child.manifest_artifact_id IS NULL THEN '[]'::jsonb
                ELSE jsonb_build_array(valid_child.manifest_artifact_id) END
    )
  )
"""


class PostgresWebFullPageSupplementalClaimRepository:
    """通过 qc_surface_phase_run 的唯一键保证所有终态均不重试。"""

    def __init__(self, *, pool=None):
        self._pool = pool

    async def claim(self, *, command, permit):
        stage = getattr(command, "stage_cursor", None)
        if stage is not None and (
            stage.current_stage is not WebRunStage.FULL_PAGE_CAPTURE
            or stage.status is not WebStageStatus.RUNNING
            or stage.run_id != command.agent_run_id
            or stage.cursor_payload.get("session_id") != command.session_id
            or stage.cursor_payload.get("capture_id") != command.source_capture_id
            or stage.cursor_payload.get("finalization_epoch") != permit.finalization_epoch
        ):
            return None
        source_eligibility = getattr(command, "source_eligibility", "formal_ready")
        if source_eligibility not in {"formal_ready", "trusted_pre_gate"}:
            raise ValueError("source_eligibility 不受支持")
        if (
            permit.task_id != command.task_id
            or permit.session_id != command.session_id
            or command.session_id != "web:" + command.agent_run_id
            or permit.current_capture_id != command.source_capture_id
        ):
            return None
        idempotency_key = f"{command.agent_run_id}:{command.source_capture_id}"
        phase_id = build_surface_phase_run_id(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_key="web_full_page_supplemental",
            idempotency_key=idempotency_key,
        )
        payload = {
            "agent_run_id": command.agent_run_id,
            "source_capture_id": command.source_capture_id,
            "source_url_sha256": sha256(command.source_url.encode()).hexdigest(),
        }
        if stage is not None:
            payload.update(
                stage_idempotency_key=stage.idempotency_key, stage_attempt_count=stage.attempt_count
            )
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            claimed_phase_id = await connection.fetchval(
                """
                INSERT INTO qc_surface_phase_run (
                    phase_run_id, task_id, session_id, surface_kind, phase_key,
                    phase_protocol_version, idempotency_key, source_capture_id,
                    status, command_sequence, finalization_epoch,
                    context_payload, context_payload_hash
                )
                SELECT $1,task.id,session.session_id,'web','web_full_page_supplemental',
                    $7,$5,capture.id,'running',session.command_sequence,
                    session.finalization_epoch,$8::jsonb,$9
                FROM qc_task task
                JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$3::uuid
                    AND run.agent_type='main' AND run.status='running'
                JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=$4
                    AND session.surface_kind='web' AND session.session_owner='web-v2-workflow'
                    AND session.status='active'
                    AND session.finalization_epoch=task.finalization_epoch
                JOIN qc_capture capture ON capture.id=$6::uuid AND capture.task_id=task.id
                    AND capture.agent_run_id=run.id
                    AND capture.surface_session_id=session.session_id
                    AND capture.surface_kind='web' AND capture.capture_role=ANY($15::text[])
                    AND capture.capture_status='captured'
                JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=task.id
                    AND page.agent_run_id=run.id AND page.surface_session_id=session.session_id
                    AND page.capture_role=ANY($15::text[]) AND page.capture_status='captured'
                WHERE task.id=$2::uuid AND task.target_platform='web' AND task.status='running'
                    AND task.task_config->>'workflow_version'='v2'
                    AND task.finalization_state='open'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest
                        WHERE latest.task_id=task.id AND latest.agent_type='main'
                        ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND page.url=$14
                    AND COALESCE(NULLIF(page.metadata->>'capture_purpose',''),
                        'web_page_ready_baseline')='web_page_ready_baseline'
                    AND page.metadata #>>
                        '{pipeline_metrics,web_readiness,settle_complete}'='true'
                    AND page.metadata #>>
                        '{pipeline_metrics,web_readiness,capture_consistent}'='true'
                    AND COALESCE(page.metadata #>>
                        '{pipeline_metrics,web_readiness,human_verification_present}',
                        'false')='false'
                    AND COALESCE(page.metadata #>>
                        '{pipeline_metrics,web_readiness,visible_loader}','false')='false'
                    AND COALESCE(page.metadata #>>
                        '{pipeline_metrics,web_readiness,transition_active}','false')='false'
                    AND EXISTS (SELECT 1 FROM qc_evidence_artifact artifact
                        WHERE artifact.task_id=page.task_id
                          AND artifact.page_capture_id=page.id
                          AND artifact.artifact_type='screenshot')
                    AND EXISTS (SELECT 1 FROM qc_evidence_artifact artifact
                        WHERE artifact.task_id=page.task_id
                          AND artifact.page_capture_id=page.id
                          AND artifact.artifact_type='dom')
                    AND $16 IN ('formal_ready','trusted_pre_gate')
                    AND session.current_capture_id=capture.id AND session.lease_token=$10::uuid
                    AND session.lease_owner=$11 AND session.lease_expires_at>now()
                    AND session.command_sequence=$12 AND session.finalization_epoch=$13
                ON CONFLICT (task_id,session_id,phase_key,idempotency_key) DO NOTHING
                RETURNING phase_run_id
                """,
                phase_id,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                idempotency_key,
                command.source_capture_id,
                WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION,
                json.dumps(payload),
                build_surface_phase_payload_hash(payload),
                permit.lease_token,
                permit.lease_owner,
                permit.command_sequence,
                permit.finalization_epoch,
                command.source_url,
                _FORMAL_READY_CAPTURE_ROLES,
                source_eligibility,
            )
            if claimed_phase_id is not None:
                return claimed_phase_id
            if stage is not None and stage.attempt_count == 2:
                return await self._claim_checkpoint_retry(
                    connection, command=command, permit=permit, phase_id=phase_id, payload=payload
                )
            # 只有 Task/Run/Session/租约/当前 Capture 仍可信，但 ready 准入或
            # URL 等业务资格不满足时，才持久化一次失败终态。并发命中既有阶段
            # 会由唯一键保持原状态，不能把 running/completed 覆盖成失败。
            await connection.execute(
                """
                INSERT INTO qc_surface_phase_run (
                    phase_run_id, task_id, session_id, surface_kind, phase_key,
                    phase_protocol_version, idempotency_key, source_capture_id,
                    status, command_sequence, finalization_epoch,
                    context_payload, context_payload_hash,
                    terminal_reason_code, evidence_artifact_ids, finished_at
                )
                SELECT $1,task.id,session.session_id,'web','web_full_page_supplemental',
                    $7,$5,capture.id,'failed',session.command_sequence,
                    session.finalization_epoch,$8::jsonb,$9,
                    'WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED','[]'::jsonb,now()
                FROM qc_task task
                JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$3::uuid
                    AND run.agent_type='main' AND run.status='running'
                JOIN qc_surface_session session ON session.task_id=task.id
                    AND session.session_id=$4 AND session.surface_kind='web'
                    AND session.session_owner='web-v2-workflow' AND session.status='active'
                    AND session.finalization_epoch=task.finalization_epoch
                JOIN qc_capture capture ON capture.id=$6::uuid AND capture.task_id=task.id
                    AND capture.agent_run_id=run.id
                    AND capture.surface_session_id=session.session_id
                    AND capture.surface_kind='web' AND capture.capture_status='captured'
                WHERE task.id=$2::uuid AND task.target_platform='web' AND task.status='running'
                    AND task.task_config->>'workflow_version'='v2'
                    AND task.finalization_state='open'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest
                        WHERE latest.task_id=task.id AND latest.agent_type='main'
                        ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND session.current_capture_id=capture.id
                    AND session.lease_token=$10::uuid AND session.lease_owner=$11
                    AND session.lease_expires_at>now()
                    AND session.command_sequence=$12 AND session.finalization_epoch=$13
                ON CONFLICT (task_id,session_id,phase_key,idempotency_key) DO NOTHING
                """,
                phase_id,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                idempotency_key,
                command.source_capture_id,
                WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION,
                json.dumps(payload),
                build_surface_phase_payload_hash(payload),
                permit.lease_token,
                permit.lease_owner,
                permit.command_sequence,
                permit.finalization_epoch,
            )
            return None

    async def _claim_checkpoint_retry(self, connection, *, command, permit, phase_id, payload):
        """只允许有效第二次阶段租约重开同一失败长图，不增加另一正式终态。"""
        stage = command.stage_cursor
        return await connection.fetchval(
            """UPDATE qc_surface_phase_run phase
               SET status='running',terminal_reason_code=NULL,finished_at=NULL,
                   command_sequence=$4,context_payload=$5::jsonb,context_payload_hash=$6,
                   updated_at=now(),row_version=phase.row_version+1
               FROM qc_web_run_stage_checkpoint checkpoint,qc_surface_session session,
                    qc_task task,qc_agent_run run
               WHERE phase.phase_run_id=$1 AND phase.status='failed'
                 AND phase.phase_key='web_full_page_supplemental'
                 AND phase.terminal_reason_code IN ('WEB_FULL_PAGE_SCREENSHOT_TIMEOUT',
                                                    'WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED')
                 AND phase.context_payload->>'stage_idempotency_key'=$2
                 AND phase.context_payload->>'stage_attempt_count'='1'
                 AND phase.finalization_epoch=$3 AND phase.source_capture_id=$7::uuid
                 AND phase.task_id=task.id AND phase.session_id=session.session_id
                 AND session.task_id=task.id AND run.id=$8::uuid AND run.task_id=task.id
                 AND run.status='running' AND task.status='running'
                 AND task.finalization_state='open'
                 AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                 AND task.finalization_epoch=$3 AND session.finalization_epoch=$3
                 AND session.status='active' AND session.session_owner='web-v2-workflow'
                 AND session.current_capture_id=phase.source_capture_id
                 AND session.lease_token=$9::uuid AND session.lease_owner=$10
                 AND session.command_sequence=$4 AND session.lease_expires_at>clock_timestamp()
                 AND checkpoint.run_id=run.id AND checkpoint.logical_page_key=$11
                 AND checkpoint.current_stage='full_page_capture'
                 AND checkpoint.stage_status='running'
                 AND checkpoint.row_version=$12 AND checkpoint.claim_token=$13::uuid
                 AND checkpoint.attempt_count=2 AND checkpoint.lease_expires_at>clock_timestamp()
               RETURNING phase.phase_run_id""",
            phase_id,
            stage.idempotency_key,
            permit.finalization_epoch,
            permit.command_sequence,
            json.dumps(payload),
            build_surface_phase_payload_hash(payload),
            command.source_capture_id,
            command.agent_run_id,
            permit.lease_token,
            permit.lease_owner,
            stage.logical_page_key,
            stage.row_version,
            stage.claim_token,
        )

    async def finish(
        self,
        *,
        phase_id,
        status,
        reason_code,
        artifact_ids,
        stage_cursor=None,
        stabilization_audit=None,
    ):
        if status not in {"completed", "failed"}:
            raise ValueError("全页补图阶段只允许明确的成功或失败终态")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            await self._finish_with_connection(
                connection,
                phase_id=phase_id,
                status=status,
                reason_code=reason_code,
                artifact_ids=artifact_ids,
                stage_cursor=stage_cursor,
                stabilization_audit=stabilization_audit,
            )

    async def read_terminal(self, *, command):
        """恢复首次终态；若子 Capture 已落盘则补偿关闭中断的 running phase。"""

        phase_id = build_surface_phase_run_id(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_key="web_full_page_supplemental",
            idempotency_key=f"{command.agent_run_id}:{command.source_capture_id}",
        )
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await self._read_scoped_phase(
                connection,
                phase_id=phase_id,
                command=command,
            )
            if row is None:
                return None
            if row["status"] == "running":
                artifact_ids = _terminal_screenshot_artifact_ids(row)
                manifest_artifact_id = _terminal_manifest_artifact_id(row)
                if not artifact_ids or not row["supplemental_capture_id"]:
                    return None
                try:
                    await self._finish_with_connection(
                        connection,
                        phase_id=phase_id,
                        status="completed",
                        reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                        artifact_ids=(
                            *artifact_ids,
                            *((manifest_artifact_id,) if manifest_artifact_id else ()),
                        ),
                    )
                except RuntimeError:
                    # 并发恢复者可能已先完成；重新读取终态后再决定是否失败。
                    pass
                row = await self._read_scoped_phase(
                    connection,
                    phase_id=phase_id,
                    command=command,
                )
                if row is None or row["status"] == "running":
                    raise RuntimeError("WEB_FULL_PAGE_SUPPLEMENTAL_RECONCILIATION_FAILED")
            if row["status"] == "failed":
                return WebFullPageSupplementalResult(
                    status="failed",
                    source_capture_id=command.source_capture_id,
                    supplemental_capture_id=None,
                    screenshot_artifact_id=None,
                    reason_code=str(
                        row["terminal_reason_code"] or "WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED"
                    ),
                )
            if row["status"] != "completed":
                return None
            artifact_ids = _terminal_screenshot_artifact_ids(row)
            manifest_artifact_id = _terminal_manifest_artifact_id(row)
            if not row["supplemental_capture_id"] or not artifact_ids:
                raise RuntimeError("WEB_FULL_PAGE_SUPPLEMENTAL_TERMINAL_EVIDENCE_INVALID")
            return WebFullPageSupplementalResult(
                status="captured",
                source_capture_id=command.source_capture_id,
                supplemental_capture_id=str(row["supplemental_capture_id"]),
                screenshot_artifact_id=artifact_ids[0],
                reason_code=str(row["terminal_reason_code"] or "WEB_FULL_PAGE_SCREENSHOT_CAPTURED"),
                screenshot_artifact_ids=artifact_ids,
                manifest_artifact_id=manifest_artifact_id,
            )

    async def _finish_with_connection(
        self,
        connection,
        *,
        phase_id,
        status,
        reason_code,
        artifact_ids,
        stage_cursor=None,
        stabilization_audit=None,
    ) -> None:
        query = _SUPPLEMENTAL_FINISH_SQL
        stage_args = ()
        if stage_cursor is not None:
            query = query.replace(
                "RETURNING phase.phase_run_id",
                """
              AND EXISTS (SELECT 1 FROM qc_web_run_stage_checkpoint checkpoint
                JOIN qc_agent_run run ON run.id=checkpoint.run_id
                JOIN qc_task task ON task.id=run.task_id
                WHERE checkpoint.run_id=$6::uuid AND checkpoint.logical_page_key=$7
                  AND checkpoint.row_version=$8 AND checkpoint.claim_token=$9::uuid
                  AND checkpoint.stage_status='running'
                  AND checkpoint.current_stage='full_page_capture'
                  AND checkpoint.lease_expires_at>clock_timestamp()
                  AND task.id=phase.task_id AND task.finalization_epoch=phase.finalization_epoch
                  AND task.status='running' AND task.finalization_state <> 'sealed'
                  AND run.status='running'
                  AND COALESCE(run.output_context->>'cancel_requested','false')='false')
              RETURNING phase.phase_run_id""",
            )
            stage_args = (
                stage_cursor.run_id,
                stage_cursor.logical_page_key,
                stage_cursor.row_version,
                stage_cursor.claim_token,
            )
        args = (
            phase_id,
            status,
            reason_code,
            json.dumps(list(artifact_ids)),
            WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION,
            *stage_args,
        )
        if stabilization_audit is not None:
            # 审计写入独立步骤；保持领取时的上下文及其摘要不可变。
            payload_slot, hash_slot = len(args) + 1, len(args) + 2
            query = (
                "WITH finished AS ("
                + query.replace("RETURNING phase.phase_run_id", "RETURNING phase.*").replace(
                    "row_version=row_version+1", "row_version=row_version+1,step_count=step_count+1"
                )
                + f"""), audit AS (
              INSERT INTO qc_surface_phase_step (
                phase_step_id,phase_run_id,task_id,session_id,step_key,step_index,
                status,decision_status,reason_code,source_capture_id,
                decision_payload,decision_payload_hash,evidence_artifact_ids)
              SELECT 'full-page:' || md5(phase_run_id || ':' || command_sequence::text),
                phase_run_id,task_id,session_id,'stabilization_audit:' || command_sequence::text,
                command_sequence,status,'audited',terminal_reason_code,source_capture_id,
                ${payload_slot}::jsonb,${hash_slot},evidence_artifact_ids
              FROM finished
              RETURNING phase_run_id
            ) SELECT phase_run_id FROM finished"""
            )
            args += (
                json.dumps(stabilization_audit),
                build_surface_phase_payload_hash(stabilization_audit),
            )
        finished = await connection.fetchrow(query, *args)
        if finished is None:
            raise RuntimeError("WEB_FULL_PAGE_SUPPLEMENTAL_FINISH_SCOPE_INVALID")

    async def _read_scoped_phase(self, connection, *, phase_id, command):
        return await connection.fetchrow(
            _SUPPLEMENTAL_READ_TERMINAL_SQL,
            phase_id,
            command.task_id,
            command.agent_run_id,
            command.session_id,
            command.source_capture_id,
            sha256(command.source_url.encode()).hexdigest(),
            WEB_FULL_PAGE_SUPPLEMENTAL_PROTOCOL_VERSION,
        )


def _terminal_screenshot_artifact_ids(row) -> tuple[str, ...]:
    try:
        raw = row["screenshot_artifact_ids"]
    except (KeyError, TypeError):
        raw = None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if isinstance(raw, (list, tuple)):
        values = tuple(str(item) for item in raw if isinstance(item, str) and item)
        if len(values) == len(raw) and len(set(values)) == len(values):
            return values
        return ()
    try:
        singular = row["screenshot_artifact_id"]
    except (KeyError, TypeError):
        singular = None
    return (str(singular),) if singular else ()


def _terminal_manifest_artifact_id(row) -> str | None:
    try:
        value = row["manifest_artifact_id"]
    except (KeyError, TypeError):
        value = None
    return str(value) if value else None


class PostgresWebAuxiliaryNavigationClaimRepository:
    """为 Web 搜索/活动 href 导航保存一次性生产阶段 claim。"""

    def __init__(self, *, pool=None):
        self._pool = pool

    async def finish_stage(
        self, *, command, cursor, status, reason_code, result_capture_id, page_type,
    ):
        """独立辅助游标只在同一有效领取下收尾；重复终态只回读，不重写。"""
        from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
        from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
            PostgresWebRunStageCheckpointRepository,
            WebStageCheckpointConflict,
        )

        expected_phase = {
            "aux:search": "web_search_discovery", "aux:activity": "web_activity_navigation",
        }.get(cursor.logical_page_key)
        if (
            expected_phase is None
            or cursor.current_stage is not WebRunStage.EVIDENCE_RECONCILIATION
            or cursor.run_id != command.agent_run_id
            or cursor.cursor_payload.get("session_id") != command.session_id
            or command.session_id != f"web:{command.agent_run_id}"
        ):
            raise WebStageCheckpointConflict("辅助收尾的阶段身份不一致")
        if status not in {"completed", "failed"} or page_type not in {
            None, "activity", "search_page", "collection_page", "unknown",
        }:
            raise ValueError("辅助导航收尾状态或页型无效")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            await PostgresWebRunStageCheckpointRepository(pool=pool)._lock_run(
                connection, cursor.run_id, cursor.cursor_payload["finalization_epoch"],
            )
            authorized = await connection.fetchval(
                """SELECT checkpoint.run_id FROM qc_web_run_stage_checkpoint checkpoint
                   JOIN qc_agent_run run ON run.id=checkpoint.run_id
                   JOIN qc_surface_session session ON session.task_id=run.task_id
                     AND session.session_id=$3 AND session.session_owner='web-v2-workflow'
                     AND session.status='active' AND session.surface_kind='web'
                     AND session.finalization_epoch=$7
                   WHERE checkpoint.run_id=$1::uuid AND run.task_id=$2::uuid
                     AND checkpoint.logical_page_key=$4 AND checkpoint.row_version=$5
                     AND checkpoint.claim_token=$6::uuid AND checkpoint.stage_status='running'
                     AND checkpoint.current_stage='evidence_reconciliation'
                     AND checkpoint.lease_expires_at>clock_timestamp()
                   FOR UPDATE OF checkpoint""",
                cursor.run_id, command.task_id, command.session_id, cursor.logical_page_key,
                cursor.row_version, cursor.claim_token, cursor.cursor_payload["finalization_epoch"],
            )
            if authorized is None:
                raise WebStageCheckpointConflict("辅助收尾的 Run、Session 或领取围栏失效")
            phase_id = cursor.cursor_payload["surface_phase_run_id"]
            phase = await connection.fetchrow(
                "SELECT * FROM qc_surface_phase_run WHERE phase_run_id=$1 FOR UPDATE", phase_id,
            )
            if phase is None:
                # 导航前被拒绝时可能尚未领取辅助 phase，不能为收尾伪造新领取。
                return
            payload = phase["context_payload"]
            payload = json.loads(payload) if isinstance(payload, str) else payload
            if (
                str(phase["task_id"]) != command.task_id
                or phase["session_id"] != command.session_id
                or phase["finalization_epoch"] != cursor.cursor_payload["finalization_epoch"]
                or phase["phase_key"] != expected_phase
                or payload.get("agent_run_id") != command.agent_run_id
                or payload.get("candidate_id") != cursor.cursor_payload["idempotency_key"]
                or str(phase["source_capture_id"]) != cursor.cursor_payload["parent_capture_id"]
            ):
                raise WebStageCheckpointConflict("辅助 phase 不属于当前冻结候选")
            if phase["status"] != "running":
                if (
                    phase["status"] == status and phase["terminal_reason_code"] == reason_code
                    and payload.get("result_capture_id") == result_capture_id
                    and payload.get("page_type") == page_type
                ):
                    return
                raise WebStageCheckpointConflict("辅助终态不可被不同结果覆盖")
            await self._finish_on_connection(
                connection, phase_id=phase_id, status=status, reason_code=reason_code,
                result_capture_id=result_capture_id, page_type=page_type,
            )

    async def claim(
        self,
        *,
        task_id,
        agent_run_id,
        session_id,
        phase_key,
        source_capture_id,
        candidate_source_capture_id,
        candidate_id,
    ):
        if phase_key not in _AUXILIARY_PHASE_KEYS:
            raise ValueError("phase_key 不受支持")
        if session_id != f"web:{agent_run_id}":
            return None
        if candidate_source_capture_id != source_capture_id:
            return None
        idempotency_key = f"{agent_run_id}:{source_capture_id}:{candidate_id}"
        phase_id = build_surface_phase_run_id(
            task_id=task_id,
            session_id=session_id,
            phase_key=phase_key,
            idempotency_key=idempotency_key,
        )
        payload = {
            "agent_run_id": agent_run_id,
            "source_capture_id": source_capture_id,
            "candidate_source_capture_id": candidate_source_capture_id,
            "candidate_id": candidate_id,
        }
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            return await connection.fetchval(
                """
                INSERT INTO qc_surface_phase_run (
                    phase_run_id,task_id,session_id,surface_kind,phase_key,
                    phase_protocol_version,idempotency_key,source_capture_id,status,
                    command_sequence,finalization_epoch,context_payload,context_payload_hash
                )
                SELECT $1,task.id,session.session_id,'web',$5,$6,$7,capture.id,'running',
                    session.command_sequence,session.finalization_epoch,$9::jsonb,$10
                FROM qc_task task
                JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$3::uuid
                    AND run.agent_type='main' AND run.status='running'
                JOIN qc_surface_session session ON session.task_id=task.id
                    AND session.session_id=$4 AND session.surface_kind='web'
                    AND session.session_owner='web-v2-workflow' AND session.status='active'
                    AND session.finalization_epoch=task.finalization_epoch
                JOIN qc_capture capture ON capture.id=$8::uuid AND capture.task_id=task.id
                    AND capture.agent_run_id=run.id
                    AND capture.surface_session_id=session.session_id
                    AND capture.surface_kind='web' AND capture.capture_role<>'supplemental'
                    AND capture.capture_status='captured'
                WHERE task.id=$2::uuid AND task.target_platform='web'
                    AND task.task_config->>'workflow_version'='v2'
                    AND task.status='running' AND task.finalization_state='open'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest
                        WHERE latest.task_id=task.id AND latest.agent_type='main'
                        ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND session.current_capture_id=capture.id
                ON CONFLICT (task_id,session_id,phase_key,idempotency_key) DO NOTHING
                RETURNING phase_run_id
                """,
                phase_id,
                task_id,
                agent_run_id,
                session_id,
                phase_key,
                _AUXILIARY_PHASE_PROTOCOL_VERSION,
                idempotency_key,
                source_capture_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                build_surface_phase_payload_hash(payload),
            )

    async def finish(
        self,
        *,
        phase_id,
        status,
        reason_code,
        result_capture_id,
        page_type,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("辅助导航阶段只允许明确终态")
        if page_type not in {None, "activity", "search_page", "collection_page", "unknown"}:
            raise ValueError("辅助导航页型不受支持")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            await self._finish_on_connection(
                connection, phase_id=phase_id, status=status, reason_code=reason_code,
                result_capture_id=result_capture_id, page_type=page_type,
            )

    async def _finish_on_connection(
        self, connection, *, phase_id, status, reason_code, result_capture_id, page_type,
    ):
        finished_phase_id = await connection.fetchval(
                """
                UPDATE qc_surface_phase_run AS phase
                SET status=$2,terminal_reason_code=$3,
                    context_payload=phase.context_payload ||
                        jsonb_build_object(
                            'result_capture_id',$4::text,
                            'page_type',$5::text
                        ),
                    finished_at=now(),updated_at=now(),row_version=row_version+1
                WHERE phase_run_id=$1 AND phase_key=ANY($6::text[]) AND status='running'
                    AND (
                        $2='failed'
                        OR (
                            $2='completed'
                            AND $4::uuid IS NOT NULL
                            AND (
                                (phase.phase_key='web_search_discovery'
                                 AND $5=ANY($7::text[]))
                                OR
                                (phase.phase_key='web_activity_navigation'
                                 AND $5=ANY($8::text[]))
                            )
                        )
                    )
                    AND ($4::uuid IS NULL OR EXISTS (
                        SELECT 1 FROM qc_capture result_capture
                        JOIN qc_page_capture result_page
                          ON result_page.id=result_capture.id
                         AND result_page.task_id=result_capture.task_id
                         AND result_page.agent_run_id=result_capture.agent_run_id
                        WHERE result_capture.id=$4::uuid
                          AND result_capture.task_id=phase.task_id
                          AND result_capture.agent_run_id=(
                            phase.context_payload->>'agent_run_id'
                          )::uuid
                          AND result_capture.surface_session_id=phase.session_id
                          AND result_capture.surface_kind='web'
                  AND result_capture.metadata->>'finalization_epoch'=phase.finalization_epoch::text
                          AND result_capture.capture_status='captured'
                          AND ($5::text IS NULL OR result_page.page_type=$5)
                    ))
                RETURNING phase.phase_run_id
                """,
                phase_id,
                status,
                reason_code,
                result_capture_id,
                page_type,
                tuple(sorted(_AUXILIARY_PHASE_KEYS)),
                ("search_page",),
                ("activity", "collection_page"),
            )
        if finished_phase_id is None:
            raise RuntimeError("WEB_AUXILIARY_FINISH_SCOPE_INVALID")
