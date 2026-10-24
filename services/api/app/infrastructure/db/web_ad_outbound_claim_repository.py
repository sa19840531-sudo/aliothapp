"""复用 Surface 阶段唯一键，永久消费每个 Run 逻辑页的一次广告直开。"""

import json
from hashlib import sha256

from services.api.app.application.web_ad_failure_diagnostics import (
    safe_ad_failure_code,
    safe_ad_failure_detail,
)
from services.api.app.application.web_ad_landing_evidence import (
    WebAdLandingCaptureError,
    WebAdLandingCaptureReceipt,
)
from services.api.app.domain.exploration.surface_phase import (
    build_surface_phase_payload_hash,
    build_surface_phase_run_id,
    build_surface_phase_step_id,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWebAdOutboundClaimRepository:
    def __init__(self, *, pool=None):
        self._pool = pool

    async def read_terminal(self, *, command):
        """已消费阶段只回读同源同 epoch 子证据；不因 Worker 重启再次直开。"""
        if command.session_id != f"web:{command.agent_run_id}":
            raise WebAdLandingCaptureError(
                reason_code="WEB_AD_SOURCE_CAPTURE_REQUIRED", message="广告 Session 归属不一致"
            )
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT phase.status,phase.terminal_reason_code,phase.phase_run_id,
                          (SELECT step.decision_payload FROM qc_surface_phase_step step
                           WHERE step.phase_run_id=phase.phase_run_id
                             AND step.step_key='terminal_failure') AS failure_detail,
                          child.id AS capture_id,
                          page.url,page.page_type,page.action_log,
                          (SELECT jsonb_agg(
                              jsonb_build_object(
                                  'id',artifact.id::text,
                                  'artifact_type',artifact.artifact_type,
                                  'metadata',artifact.metadata
                              ) ORDER BY artifact.created_at,artifact.id
                           ) FROM qc_evidence_artifact artifact
                           WHERE artifact.page_capture_id=child.id
                             AND artifact.task_id=phase.task_id
                          ) AS artifacts
                   FROM qc_surface_phase_run phase
                   JOIN qc_task task ON task.id=phase.task_id
                   JOIN qc_surface_session session ON session.task_id=phase.task_id
                     AND session.session_id=phase.session_id
                     AND session.finalization_epoch=phase.finalization_epoch
                     AND session.session_owner='web-v2-workflow'
                   JOIN qc_capture source ON source.id=phase.source_capture_id
                     AND source.task_id=phase.task_id AND source.agent_run_id=$3::uuid
                     AND source.surface_session_id=phase.session_id
                     AND source.capture_status='captured'
                     AND source.metadata->>'finalization_epoch'=phase.finalization_epoch::text
                   LEFT JOIN qc_capture child ON child.parent_capture_id=source.id
                     AND child.task_id=phase.task_id AND child.agent_run_id=$3::uuid
                     AND child.surface_session_id=phase.session_id AND child.surface_kind='web'
                     AND child.capture_status='captured' AND child.capture_role='supplemental'
                     AND child.session_command_sequence=phase.command_sequence
                     AND child.metadata->>'finalization_epoch'=phase.finalization_epoch::text
                     AND child.metadata->>'capture_purpose'='web_ad_page'
                   LEFT JOIN qc_page_capture page ON page.id=child.id AND page.task_id=phase.task_id
                     AND page.agent_run_id=$3::uuid AND page.parent_page_capture_id=source.id
                     AND page.session_command_sequence=phase.command_sequence
                     AND page.metadata->>'finalization_epoch'=phase.finalization_epoch::text
                   WHERE phase.task_id=$1::uuid AND phase.session_id=$2
                     AND phase.phase_key='web_ad_outbound'
                     AND phase.phase_protocol_version='web-ad-outbound.v1'
                     AND phase.idempotency_key=$4 AND phase.source_capture_id=$5::uuid
                     AND phase.finalization_epoch=task.finalization_epoch
                   ORDER BY child.created_at DESC NULLS LAST LIMIT 1""",
                command.task_id,
                command.session_id,
                command.agent_run_id,
                command.agent_run_id + ":" + command.logical_page_id,
                command.source_capture_id,
            )
        if row is None:
            return None
        if (
            row.get("failure_detail")
            or row["capture_id"] is None
            or row["page_type"] != "ad_page"
        ):
            detail = row.get("failure_detail") or {}
            detail = json.loads(detail) if isinstance(detail, str) else detail
            raise WebAdLandingCaptureError(
                reason_code=row["terminal_reason_code"] or "WEB_AD_LANDING_ALREADY_ATTEMPTED",
                message="已消费广告阶段没有完整落地证据，禁止重开",
                detail=safe_ad_failure_detail(detail, phase_id=row.get("phase_run_id")),
            )
        trace = row["action_log"]
        trace = json.loads(trace) if isinstance(trace, str) else trace
        action = next(
            (
                item
                for item in reversed(trace or [])
                if item.get("action") == "direct_ad_outbound"
                or (
                    item.get("action") == "navigate"
                    and item.get("navigation_mode") == "isolated_context_goto"
                )
            ),
            {},
        )
        navigation = action.get("navigation") or {}
        evidence = action.get("ad_navigation_evidence") or {}
        landing = evidence.get("landing") or {}
        restored = (
            navigation.get("new_page_count") == 1
            and navigation.get("closed_after_observation_count") == 1
            and navigation.get("current_url_changed") is not True
            and navigation.get("source_context_contaminated") is not True
        )
        artifacts = row["artifacts"] or []
        artifacts = json.loads(artifacts) if isinstance(artifacts, str) else artifacts
        artifact_ids, artifact_refs = _rebuild_ad_artifact_refs(artifacts)
        if (
            not restored
            or not artifact_refs
            or evidence.get("status") not in {"observed", "observed_unreachable"}
        ):
            raise WebAdLandingCaptureError(
                reason_code="WEB_AD_LANDING_OPEN_EVIDENCE_INVALID", message="广告隔离关闭证据不完整"
            )
        return WebAdLandingCaptureReceipt(
            capture_id=str(row["capture_id"]),
            landing_url=row["url"],
            artifact_ids=artifact_ids,
            artifact_refs=artifact_refs,
            source_restored=True,
            landing_reachable=landing.get("reachable") is not False,
            reason_code=landing.get("reason_code"),
            requested_url=command.destination_url,
        )

    async def claim(self, *, command, permit):
        if (
            permit.task_id != command.task_id
            or permit.session_id != command.session_id
            or command.session_id != "web:" + command.agent_run_id
        ):
            return None
        key = command.agent_run_id + ":" + command.logical_page_id
        phase_id = build_surface_phase_run_id(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_key="web_ad_outbound",
            idempotency_key=key,
        )
        payload = {
            "agent_run_id": command.agent_run_id,
            "logical_page_id": command.logical_page_id,
            "source_url_sha256": sha256(command.source_url.encode()).hexdigest(),
            "destination_url_sha256": sha256(command.destination_url.encode()).hexdigest(),
            "page_role": command.page_role,
        }
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            return await connection.fetchval(
                """
                INSERT INTO qc_surface_phase_run (
                    phase_run_id, task_id, session_id, surface_kind, phase_key,
                    phase_protocol_version, idempotency_key, source_capture_id,
                    status, command_sequence, finalization_epoch,
                    context_payload, context_payload_hash
                )
                SELECT $1,task.id,session.session_id,'web','web_ad_outbound',
                    'web-ad-outbound.v1',$5,capture.id,'running',session.command_sequence,
                    session.finalization_epoch,$7::jsonb,$8
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
                    AND capture.surface_kind='web' AND capture.capture_status='captured'
                JOIN qc_capture current_capture ON current_capture.id=session.current_capture_id
                    AND current_capture.task_id=task.id AND current_capture.agent_run_id=run.id
                    AND current_capture.surface_session_id=session.session_id
                    AND current_capture.surface_kind='web'
                    AND current_capture.capture_status='captured'
                    AND current_capture.metadata->>'finalization_epoch'=
                        session.finalization_epoch::text
                JOIN qc_page_capture current_page ON current_page.id=current_capture.id
                    AND current_page.task_id=task.id AND current_page.agent_run_id=run.id
                    AND current_page.surface_session_id=session.session_id
                    AND current_page.capture_status='captured'
                WHERE task.id=$2::uuid AND task.target_platform='web' AND task.status='running'
                    AND task.task_config->>'workflow_version'='v2'
                    AND task.finalization_state='open'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest
                        WHERE latest.task_id=task.id AND latest.agent_type='main'
                        ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND session.current_capture_id=$13::uuid AND current_page.url=$14
                    AND session.lease_token=$9::uuid
                    AND session.lease_owner=$10 AND session.lease_expires_at>now()
                    AND session.command_sequence=$11 AND session.finalization_epoch=$12
                ON CONFLICT (task_id,session_id,phase_key,idempotency_key) DO NOTHING
                RETURNING phase_run_id
                """,
                phase_id,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                key,
                command.source_capture_id,
                json.dumps(payload),
                build_surface_phase_payload_hash(payload),
                permit.lease_token,
                permit.lease_owner,
                permit.command_sequence,
                permit.finalization_epoch,
                permit.current_capture_id,
                command.source_url,
            )

    async def finish(self, *, phase_id, status, reason_code, artifact_ids, detail=None):
        if status not in {"completed", "failed"}:
            raise ValueError("广告阶段只允许明确的成功或失败终态")
        reason_code = safe_ad_failure_code(reason_code)
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            diagnostic = safe_ad_failure_detail(detail)
            if status == "failed" and diagnostic:
                # 终态与不可变诊断步骤原子提交，不改领取上下文及其哈希。
                await connection.execute(
                    """
                    WITH finished AS (
                        UPDATE qc_surface_phase_run
                        SET status=$2,terminal_reason_code=$3,evidence_artifact_ids=$4::jsonb,
                            finished_at=now(),updated_at=now(),row_version=row_version+1,
                            step_count=step_count+1
                        WHERE phase_run_id=$1 AND phase_key='web_ad_outbound' AND status='running'
                        RETURNING *
                    )
                    INSERT INTO qc_surface_phase_step (
                        phase_step_id,phase_run_id,task_id,session_id,step_key,step_index,
                        status,decision_status,reason_code,source_capture_id,
                        decision_payload,decision_payload_hash,evidence_artifact_ids
                    )
                    SELECT $5,phase_run_id,task_id,session_id,'terminal_failure',step_count,
                        'failed','failed',$3,source_capture_id,$6::jsonb,$7,$4::jsonb
                    FROM finished
                    """,
                    phase_id, status, reason_code, json.dumps(list(artifact_ids)),
                    build_surface_phase_step_id(
                        phase_run_id=phase_id, step_key="terminal_failure",
                    ),
                    json.dumps(diagnostic), build_surface_phase_payload_hash(diagnostic),
                )
                return
            await connection.execute(
                """
                UPDATE qc_surface_phase_run SET status=$2,terminal_reason_code=$3,
                    evidence_artifact_ids=$4::jsonb,finished_at=now(),updated_at=now(),row_version=row_version+1
                WHERE phase_run_id=$1 AND phase_key='web_ad_outbound' AND status='running'
                """,
                phase_id,
                status,
                reason_code,
                json.dumps(list(artifact_ids)),
            )


def _rebuild_ad_artifact_refs(
    artifacts: object,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """从同一广告子 Capture 的持久元数据恢复七类角色引用。"""

    if not isinstance(artifacts, list):
        return (), ()
    refs: dict[str, str] = {}
    artifact_ids: list[str] = []
    for raw in artifacts:
        if not isinstance(raw, dict):
            return (), ()
        artifact_id = str(raw.get("id") or "").strip()
        artifact_type = str(raw.get("artifact_type") or "").strip()
        metadata = raw.get("metadata") or {}
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
        if not artifact_id or not artifact_type or not isinstance(metadata, dict):
            return (), ()
        artifact_ids.append(artifact_id)
        purpose = str(metadata.get("purpose") or "")
        if artifact_type == "dom":
            refs.setdefault("landing_dom" if purpose == "ad_landing" else "source_dom", artifact_id)
        elif artifact_type == "screenshot" and purpose == "ad_landing_consistency":
            refs["source_screenshot"] = artifact_id
        elif artifact_type == "screenshot" and purpose == "ad_landing":
            refs["landing_screenshot"] = artifact_id
        elif artifact_type == "ui_snapshot" and purpose == "ad_landing":
            refs.setdefault("landing_ui_snapshot", artifact_id)
        elif artifact_type == "har" and purpose == "ad_landing":
            refs.setdefault("landing_har", artifact_id)
        elif artifact_type == "action_trace":
            refs.setdefault("landing_trace", artifact_id)
    ordered_keys = (
        "source_dom",
        "source_screenshot",
        "landing_dom",
        "landing_screenshot",
        "landing_ui_snapshot",
        "landing_har",
        "landing_trace",
    )
    if len(set(artifact_ids)) != len(artifact_ids) or any(
        not refs.get(key) for key in ordered_keys
    ):
        return (), ()
    return tuple(artifact_ids), tuple((key, refs[key]) for key in ordered_keys)
