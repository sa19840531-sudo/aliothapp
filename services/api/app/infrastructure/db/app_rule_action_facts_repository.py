"""读取当前 App 动作与可信前后材料；不读过程日志，不补写任何证据。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import PurePosixPath

from services.api.app.application.manifest_backed_packet import verify_artifact_payload
from services.api.app.core.config import get_settings
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

logger = logging.getLogger(__name__)


class PostgresAppRuleActionFactsReader:
    """同一读端口供发布与探索事实使用，只保留当前及紧邻前一条动作。"""

    def __init__(self, *, database_url=None, pool=None, artifact_reader=None):
        self._database_url = database_url
        self._pool = pool
        self._artifact_reader = artifact_reader

    async def get_facts(self, *, task_id: str, capture_id: str, agent_run_id: str):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            return await self.read_on_connection(
                connection, task_id=task_id, capture_id=capture_id, agent_run_id=agent_run_id
            )

    async def read_on_connection(self, connection, *, task_id, capture_id, agent_run_id):
        primary = await connection.fetchrow(
            f"""
            SELECT task.id::text AS task_id, run.id::text AS agent_run_id,
                   session.session_id, task.finalization_epoch,
                   capture.id::text AS capture_id, capture.session_command_sequence
            FROM qc_task AS task
            JOIN qc_agent_run AS run ON run.task_id=task.id AND run.id=$3::uuid
            JOIN qc_capture AS capture
              ON capture.task_id=task.id AND capture.agent_run_id=run.id AND capture.id=$2::uuid
             AND capture.capture_status='captured' AND capture.surface_kind IN ('android','ios')
            JOIN qc_surface_session AS session
              ON session.task_id=task.id AND session.session_id=capture.surface_session_id
             AND session.surface_kind=capture.surface_kind
             AND session.finalization_epoch=task.finalization_epoch
            WHERE task.id=$1::uuid AND task.target_platform='app'
              AND {capture_epoch_sql("capture")}=task.finalization_epoch
            """,
            task_id,
            capture_id,
            agent_run_id,
        )
        if primary is None:
            return None
        scope = {
            key: primary[key]
            for key in ("task_id", "agent_run_id", "session_id", "finalization_epoch")
        }
        sequence = primary["session_command_sequence"]
        if sequence is None:
            sequence = 0
        rows = await connection.fetch(
            f"""
            SELECT capture.id::text AS capture_id, capture.task_id::text AS task_id,
                   capture.agent_run_id::text AS agent_run_id,
                   capture.surface_session_id AS session_id,
                   {capture_epoch_sql("capture")} AS finalization_epoch,
                   capture.capture_role, capture.capture_status, capture.surface_kind,
                   capture.parent_capture_id::text AS parent_capture_id,
                   capture.action_execution_id, capture.session_command_sequence,
                   app.package_name, app.image_signature,
                   app.screenshot_artifact_id::text AS screenshot_artifact_id,
                   visual.visual_page_type AS page_type, visual.page_state,
                   visual.overlay_signature, visual.surface_facts, manifest.manifest_payload
            FROM qc_capture AS capture
            JOIN qc_app_capture AS app ON app.capture_id=capture.id
            JOIN LATERAL (
                SELECT observation.* FROM qc_app_visual_observation AS observation
                WHERE observation.task_id=capture.task_id AND observation.capture_id=capture.id
                  AND observation.session_id=capture.surface_session_id
                  AND observation.screenshot_artifact_id=app.screenshot_artifact_id
                ORDER BY observation.created_at DESC, observation.observation_id DESC LIMIT 1
            ) AS visual ON TRUE
            JOIN LATERAL (
                SELECT candidate.manifest_payload, candidate.quality_status
                FROM qc_evidence_manifest AS candidate
                WHERE candidate.task_id=capture.task_id AND candidate.capture_id=capture.id
                  AND candidate.session_id=capture.surface_session_id
                ORDER BY candidate.manifest_version DESC LIMIT 1
            ) AS manifest ON manifest.quality_status='pass'
            WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid
              AND capture.surface_session_id=$3 AND {capture_epoch_sql("capture")}=$4
              AND capture.capture_status='captured' AND capture.surface_kind IN ('android','ios')
              AND (capture.id=$5::uuid OR capture.session_command_sequence BETWEEN $6 AND $7)
            """,
            task_id,
            agent_run_id,
            scope["session_id"],
            scope["finalization_epoch"],
            capture_id,
            max(0, sequence - 1),
            sequence,
        )
        captures = []
        for raw in rows:
            row = dict(raw)
            manifest = EvidenceManifestReference.from_dict(_json(row.pop("manifest_payload")))
            if (
                manifest.task_id != task_id
                or manifest.capture_id != row["capture_id"]
                or manifest.session_id != scope["session_id"]
                or manifest.quality_status.value != "pass"
            ):
                continue
            row["manifest_quality_status"] = manifest.quality_status.value
            row["manifest_screenshot_artifact_ids"] = [
                item.artifact_id
                for item in manifest.artifacts
                if item.artifact_type == "screenshot" and item.checksum == row["image_signature"]
            ]
            row["surface_facts"] = _json(row["surface_facts"])
            row["ui_snapshot"] = await self._read_ui_snapshot(manifest)
            captures.append(row)
        action_rows = await connection.fetch(
            f"""
            SELECT action.action_execution_id, action.task_id::text AS task_id,
                   action.session_id, action.finalization_epoch, action.command_sequence,
                   action.source_capture_id::text AS source_capture_id,
                   action.result_capture_id::text AS result_capture_id, action.status,
                   action.intent_payload, action.result_payload,
                   requested.id::text AS requested_capture_id, requested_app.image_signature,
                   requested_visual.candidate_targets
            FROM qc_action_execution AS action
            LEFT JOIN qc_capture AS requested
              ON requested.id=action.requested_source_capture_id
             AND requested.task_id=action.task_id
             AND requested.agent_run_id=$2::uuid AND requested.surface_session_id=action.session_id
             AND {capture_epoch_sql("requested")}=action.finalization_epoch
            LEFT JOIN qc_app_capture AS requested_app ON requested_app.capture_id=requested.id
            LEFT JOIN LATERAL (
                SELECT observation.candidate_targets FROM qc_app_visual_observation AS observation
                WHERE observation.task_id=action.task_id AND observation.capture_id=requested.id
                  AND observation.session_id=action.session_id
                  AND observation.screenshot_artifact_id=requested_app.screenshot_artifact_id
                ORDER BY observation.created_at DESC, observation.observation_id DESC LIMIT 1
            ) AS requested_visual ON TRUE
            WHERE action.task_id=$1::uuid AND action.session_id=$3 AND action.finalization_epoch=$4
              AND action.command_sequence BETWEEN $5 AND $6
            """,
            task_id,
            agent_run_id,
            scope["session_id"],
            scope["finalization_epoch"],
            max(0, sequence - 1),
            sequence,
        )
        actions = []
        for raw in action_rows:
            row = dict(raw)
            intent = _json(row.pop("intent_payload"))
            result = _json(row.pop("result_payload"))
            row.update(
                agent_run_id=agent_run_id,
                action_type=intent.get("action_type"),
                semantic_target=intent.get("semantic_target"),
                execution_outcome=result.get("execution_outcome"),
                verification_outcome=result.get("verification_outcome"),
            )
            candidate_id = _json(intent.get("parameters")).get("candidate_id")
            targets = _json(row.pop("candidate_targets"), fallback=[])
            target = next(
                (
                    item
                    for item in targets
                    if isinstance(item, Mapping)
                    and item.get("candidate_id") == candidate_id
                    and item.get("source_capture_id") == row["requested_capture_id"]
                    and item.get("source_image_signature") == row["image_signature"]
                ),
                {},
            )
            row["target_expected_state_hint"] = target.get("expected_state_hint")
            actions.append(row)
        return {
            "scope": scope,
            "primary_capture_id": capture_id,
            "captures": captures,
            "actions": actions,
        }

    async def _read_ui_snapshot(self, manifest):
        references = [item for item in manifest.artifacts if item.artifact_type == "ui_snapshot"]
        if len(references) != 1:
            return {}
        reference = references[0]
        if tuple(PurePosixPath(reference.storage_path).parts[:2]) != (
            manifest.task_id,
            manifest.capture_id,
        ):
            return {}
        if self._artifact_reader is None:
            self._artifact_reader = build_evidence_artifact_store(get_settings().evidence_storage)
        try:
            payload = await self._artifact_reader.read(reference.storage_path)
            if not verify_artifact_payload(payload, reference=reference):
                return {}
            parsed = json.loads(payload.decode("utf-8", errors="strict"))
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except Exception:  # noqa: BLE001 - 当前制品不可读保留该材料缺口，其他动作身份仍可验证
            logger.warning("App 业务动作快照读取失败 capture_id=%s", manifest.capture_id)
            return {}


def _json(value, fallback=None):
    if isinstance(value, str):
        return json.loads(value)
    if value is None:
        return {} if fallback is None else fallback
    return value
