"""只从同一已完成动作的可信前后清单读取场景，不复用旁路过程日志。"""

import json
from typing import Any

from services.api.app.application.rule_page_state import AppActionBeforeScene
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.pool import get_postgres_pool


def startup_result_pair_candidate_sql() -> str:
    """仅预选当前完整开屏动作；正式发布仍重验清单 payload 与截图归属。"""
    return f"""
        EXISTS (
            SELECT 1 FROM qc_capture AS startup_before
            JOIN qc_app_capture AS startup_app ON startup_app.capture_id=startup_before.id
            JOIN LATERAL (
                SELECT observation.visual_page_type FROM qc_app_visual_observation AS observation
                WHERE observation.capture_id=startup_before.id
                  AND observation.task_id=task.id AND observation.session_id=session.session_id
                  AND observation.screenshot_artifact_id=startup_app.screenshot_artifact_id
                ORDER BY observation.created_at DESC, observation.observation_id DESC LIMIT 1
            ) AS startup_visual ON startup_visual.visual_page_type='startup_ad'
            JOIN qc_action_execution AS startup_action
              ON startup_action.action_execution_id=capture.action_execution_id
             AND startup_action.task_id=task.id AND startup_action.session_id=session.session_id
             AND startup_action.source_capture_id=startup_before.id
             AND startup_action.result_capture_id=capture.id AND startup_action.status='completed'
             AND startup_action.command_sequence=capture.session_command_sequence
             AND startup_action.finalization_epoch=task.finalization_epoch
            JOIN LATERAL (
                SELECT quality_status FROM qc_evidence_manifest
                WHERE task_id=task.id AND session_id=session.session_id
                  AND capture_id=startup_before.id ORDER BY manifest_version DESC LIMIT 1
            ) AS startup_manifest ON startup_manifest.quality_status='pass'
            JOIN LATERAL (
                SELECT quality_status FROM qc_evidence_manifest
                WHERE task_id=task.id AND session_id=session.session_id
                  AND capture_id=capture.id ORDER BY manifest_version DESC LIMIT 1
            ) AS startup_result_manifest ON startup_result_manifest.quality_status='pass'
            WHERE capture.capture_role='after_action'
              AND startup_before.id=capture.parent_capture_id
              AND startup_before.task_id=task.id AND startup_before.agent_run_id=run.id
              AND startup_before.surface_session_id=session.session_id
              AND startup_before.surface_kind=capture.surface_kind
              AND startup_before.capture_role='before_action'
              AND startup_before.capture_status='captured'
              AND startup_before.action_execution_id=capture.action_execution_id
              AND startup_before.session_command_sequence=capture.session_command_sequence
              AND {capture_epoch_sql('startup_before')}=task.finalization_epoch
              AND {capture_epoch_sql('capture')}=task.finalization_epoch
        )
    """


class PostgresAppActionBeforeSceneReader:
    """动作后帧恢复动作前触发状态；原始前后截图归属保持不变。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None):
        self._database_url = database_url
        self._pool = pool

    async def get_before_scene(
        self,
        *,
        task_id: str,
        capture_id: str,
        agent_run_id: str,
    ) -> AppActionBeforeScene | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT before_capture.id::text AS before_capture_id,
                       visual.visual_page_type, visual.page_state,
                       before_app.screenshot_artifact_id::text AS before_screenshot_id,
                       after_app.screenshot_artifact_id::text AS after_screenshot_id,
                       session.session_id, before_manifest.manifest_payload AS before_manifest,
                       after_manifest.manifest_payload AS after_manifest
                FROM qc_task AS task
                JOIN qc_agent_run AS run ON run.task_id=task.id AND run.id=$3::uuid
                JOIN qc_capture AS after_capture
                  ON after_capture.task_id=task.id AND after_capture.agent_run_id=run.id
                 AND after_capture.id=$2::uuid AND after_capture.capture_role='after_action'
                 AND after_capture.capture_status='captured'
                 AND after_capture.surface_kind IN ('android','ios')
                JOIN qc_capture AS before_capture
                  ON before_capture.id=after_capture.parent_capture_id
                 AND before_capture.task_id=task.id AND before_capture.agent_run_id=run.id
                 AND before_capture.surface_session_id=after_capture.surface_session_id
                 AND before_capture.surface_kind=after_capture.surface_kind
                 AND before_capture.capture_role='before_action'
                 AND before_capture.capture_status='captured'
                 AND before_capture.action_execution_id=after_capture.action_execution_id
                 AND before_capture.session_command_sequence=after_capture.session_command_sequence
                JOIN qc_surface_session AS session
                  ON session.task_id=task.id AND session.session_id=after_capture.surface_session_id
                 AND session.surface_kind=after_capture.surface_kind
                 AND session.finalization_epoch=task.finalization_epoch
                JOIN qc_action_execution AS action
                  ON action.task_id=task.id AND action.session_id=session.session_id
                 AND action.action_execution_id=after_capture.action_execution_id
                 AND action.source_capture_id=before_capture.id
                 AND action.result_capture_id=after_capture.id AND action.status='completed'
                 AND action.command_sequence=after_capture.session_command_sequence
                 AND action.finalization_epoch=task.finalization_epoch
                JOIN qc_app_capture AS before_app ON before_app.capture_id=before_capture.id
                JOIN qc_app_capture AS after_app ON after_app.capture_id=after_capture.id
                JOIN LATERAL (
                    SELECT observation.visual_page_type, observation.page_state
                    FROM qc_app_visual_observation AS observation
                    WHERE observation.task_id=task.id AND observation.session_id=session.session_id
                      AND observation.capture_id=before_capture.id
                      AND observation.screenshot_artifact_id=before_app.screenshot_artifact_id
                    ORDER BY observation.created_at DESC, observation.observation_id DESC LIMIT 1
                ) AS visual ON TRUE
                JOIN LATERAL (
                    SELECT manifest_payload, quality_status FROM qc_evidence_manifest
                    WHERE task_id=task.id AND session_id=session.session_id
                      AND capture_id=before_capture.id ORDER BY manifest_version DESC LIMIT 1
                ) AS before_manifest ON before_manifest.quality_status='pass'
                JOIN LATERAL (
                    SELECT manifest_payload, quality_status FROM qc_evidence_manifest
                    WHERE task_id=task.id AND session_id=session.session_id
                      AND capture_id=after_capture.id ORDER BY manifest_version DESC LIMIT 1
                ) AS after_manifest ON after_manifest.quality_status='pass'
                WHERE task.id=$1::uuid AND task.target_platform='app' AND task.status='running'
                  AND task.finalization_state IN ('open','exploration_draining','session_draining')
                  AND {capture_epoch_sql("before_capture")}=task.finalization_epoch
                  AND {capture_epoch_sql("after_capture")}=task.finalization_epoch
                """,
                task_id,
                capture_id,
                agent_run_id,
            )
        if row is None or not row["page_state"]:
            return None
        for role, expected_capture, screenshot_id in (
            ("before", row["before_capture_id"], row["before_screenshot_id"]),
            ("after", capture_id, row["after_screenshot_id"]),
        ):
            payload = row[f"{role}_manifest"]
            manifest = EvidenceManifestReference.from_dict(
                json.loads(payload) if isinstance(payload, str) else dict(payload)
            )
            if (
                manifest.task_id != task_id
                or manifest.capture_id != expected_capture
                or manifest.session_id != row["session_id"]
                or manifest.quality_status.value != "pass"
                or screenshot_id
                not in {
                    artifact.artifact_id
                    for artifact in manifest.artifacts
                    if artifact.artifact_type == "screenshot"
                }
            ):
                return None
        return AppActionBeforeScene(
            capture_id=row["before_capture_id"],
            page_type=row["visual_page_type"],
            page_state=row["page_state"],
        )
