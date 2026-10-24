"""不可变 App Screenshot Understanding 视觉事实仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from services.api.app.application.app_capture_evidence_fields import (
    SESSION_AUTH_UNKNOWN,
    resolve_session_auth_from_facts,
)
from services.api.app.domain.evidence import (
    EvidenceManifestReference,
    build_evidence_bundle_ready,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualValidationError,
)
from services.api.app.domain.interaction.validation import require_text
from services.api.app.domain.protocol import (
    build_protocol_envelope,
    require_aware_datetime,
    thaw_json_mapping,
)
from services.api.app.infrastructure.db.app_action_scene_repository import (
    startup_result_pair_candidate_sql,
)
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_PAGE_GRAPH_UNSET = object()


class AppVisualObservationPersistenceError(RuntimeError):
    """视觉事实归属无效或相同 Capture 的 result hash 发生漂移。"""


@dataclass(frozen=True, kw_only=True)
class SavedAppVisualObservation:
    """已持久化视觉事实及本次是否新建。"""

    observation_id: str
    analysis: AppVisualAnalysis
    observed_at: datetime
    created: bool

    def __post_init__(self) -> None:
        require_text(self.observation_id, "observation_id")
        if not isinstance(self.analysis, AppVisualAnalysis):
            raise TypeError("analysis 必须为 AppVisualAnalysis")
        require_aware_datetime(self.observed_at, "observed_at")
        if not isinstance(self.created, bool):
            raise TypeError("created 必须为布尔值")


class PostgresAppVisualObservationRepository:
    """以 Capture ID 唯一、以 result hash 锁定视觉事实。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        page_graph_repository: Any = _PAGE_GRAPH_UNSET,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._page_graph_repository = (
            page_graph_repository
            if page_graph_repository is not _PAGE_GRAPH_UNSET
            else PostgresPageGraphRepository(database_url=database_url, pool=pool)
        )

    async def get_by_capture_id(
        self,
        *,
        capture_id: str,
    ) -> SavedAppVisualObservation | None:
        require_text(capture_id, "capture_id")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    visual.observation_id::text AS observation_id,
                    visual.contract_version,
                    visual.capture_id::text AS capture_id,
                    visual.screenshot_artifact_id::text AS screenshot_artifact_id,
                    app_capture.image_signature,
                    app_capture.width,
                    app_capture.height,
                    app_capture.orientation,
                    app_capture.package_name,
                    app_capture.activity,
                    visual.visual_page_type,
                    visual.visual_summary,
                    visual.key_elements,
                    visual.candidate_targets,
                    visual.page_state,
                    visual.overlay_signature,
                    visual.surface_facts,
                    visual.provider_id,
                    visual.model_id,
                    visual.result_hash,
                    visual.created_at AS observed_at
                FROM qc_app_visual_observation AS visual
                INNER JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = visual.capture_id
                 AND app_capture.screenshot_artifact_id = visual.screenshot_artifact_id
                WHERE visual.capture_id = $1::uuid
                """,
                capture_id,
            )
        if row is None:
            return None
        return SavedAppVisualObservation(
            observation_id=str(row["observation_id"]),
            analysis=_analysis_from_row(row),
            observed_at=row["observed_at"],
            created=False,
        )

    async def save(self, analysis: AppVisualAnalysis) -> SavedAppVisualObservation:
        if not isinstance(analysis, AppVisualAnalysis):
            raise TypeError("analysis 必须为 AppVisualAnalysis")
        observation_id = str(uuid5(NAMESPACE_URL, f"alioth:app-visual:{analysis.capture_id}"))
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_visual_observation AS existing (
                    observation_id, task_id, session_id, capture_id,
                    screenshot_artifact_id, contract_version,
                    provider_id, model_id, result_hash,
                    visual_page_type, visual_summary,
                    key_elements, candidate_targets,
                    page_state, overlay_signature, surface_facts
                )
                SELECT
                    $1::uuid, capture.task_id, capture.surface_session_id,
                    capture.id, artifact.id, $4, $5, $6, $7, $8, $9,
                    $10::jsonb, $11::jsonb, $12::varchar, $13::text, $14::jsonb
                FROM qc_capture AS capture
                INNER JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = capture.id
                 AND app_capture.screenshot_artifact_id = $3::uuid
                INNER JOIN qc_evidence_artifact AS artifact
                  ON artifact.id = app_capture.screenshot_artifact_id
                 AND artifact.task_id = capture.task_id
                 AND artifact.page_capture_id = capture.id
                 AND artifact.artifact_type = 'screenshot'
                 AND artifact.content_type = 'image/png'
                 AND artifact.checksum = app_capture.image_signature
                WHERE capture.id = $2::uuid
                  AND capture.surface_kind IN ('android', 'ios')
                  AND capture.capture_status = 'captured'
                  AND capture.surface_session_id IS NOT NULL
                ON CONFLICT (capture_id) DO UPDATE
                SET capture_id = existing.capture_id
                WHERE existing.result_hash = EXCLUDED.result_hash
                  AND existing.screenshot_artifact_id = EXCLUDED.screenshot_artifact_id
                  AND existing.contract_version = EXCLUDED.contract_version
                  AND existing.provider_id = EXCLUDED.provider_id
                  AND existing.model_id = EXCLUDED.model_id
                RETURNING
                    existing.observation_id::text AS observation_id,
                    existing.created_at AS observed_at,
                    existing.surface_facts,
                    (xmax = 0) AS created
                """,
                observation_id,
                analysis.capture_id,
                analysis.screenshot_artifact_id,
                analysis.contract_version,
                analysis.provider_id,
                analysis.model_id,
                analysis.result_hash,
                analysis.visual_page_type,
                analysis.visual_summary,
                _json_dumps(list(analysis.key_elements)),
                _json_dumps([item.to_dict() for item in analysis.candidates]),
                analysis.page_state,
                analysis.overlay_signature,
                _json_dumps(thaw_json_mapping(analysis.surface_facts)),
            )
            if row is not None:
                session_auth = resolve_session_auth_from_facts(
                    surface_facts=_json_object(row["surface_facts"], "surface_facts"),
                )
                if session_auth != SESSION_AUTH_UNKNOWN:
                    # 截图先落库、视觉后完成；补同帧未知值并撤销旧候选缓存，允许增量重发。
                    # 已有工作项依靠发布幂等去重，Driver 明确事实不得被视觉覆盖。
                    await connection.execute(
                        """
                        SELECT pg_advisory_xact_lock(
                            hashtext('app-evidence-index:' || capture.agent_run_id::text)
                        )
                        FROM qc_capture AS capture
                        WHERE capture.id = $1::uuid
                        """,
                        analysis.capture_id,
                    )
                    await connection.execute(
                        """
                        UPDATE qc_capture AS capture
                        SET metadata = (COALESCE(capture.metadata, '{}'::jsonb)
                                        - 'inspection_status' - 'inspection')
                                       || jsonb_build_object('session_auth', $3::text)
                        FROM qc_app_visual_observation AS visual
                        WHERE capture.id = $1::uuid
                          AND visual.observation_id = $2::uuid
                          AND visual.capture_id = capture.id
                          AND visual.task_id = capture.task_id
                          AND visual.session_id = capture.surface_session_id
                          AND NOT EXISTS (
                              SELECT 1 FROM qc_app_evidence_index AS evidence_index
                              WHERE evidence_index.agent_run_id = capture.agent_run_id
                                AND evidence_index.task_id = capture.task_id
                                AND evidence_index.status = 'sealed'
                          )
                          AND LOWER(COALESCE(capture.metadata->>'session_auth', 'unknown'))
                              IN ('', 'unknown')
                        """,
                        analysis.capture_id,
                        str(row["observation_id"]),
                        session_auth,
                    )
            if row is not None and self._page_graph_repository is not None:
                await self._page_graph_repository.project_visual_observation_on_connection(
                    connection,
                    capture_id=analysis.capture_id,
                    visual_page_type=analysis.visual_page_type,
                    page_state=analysis.page_state,
                    overlay_signature=analysis.overlay_signature,
                    surface_facts=thaw_json_mapping(analysis.surface_facts),
                )
            if row is not None and bool(row["created"]):
                await _publish_visual_inspection_ready(
                    connection,
                    analysis=analysis,
                    observation_id=str(row["observation_id"]),
                    observed_at=row["observed_at"],
                )
        if row is None:
            raise AppVisualObservationPersistenceError(
                "App 视觉事实归属无效或相同 Capture 的 result hash 冲突"
            )
        return SavedAppVisualObservation(
            observation_id=str(row["observation_id"]),
            analysis=analysis,
            observed_at=row["observed_at"],
            created=bool(row["created"]),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def _publish_visual_inspection_ready(
    connection: Any,
    *,
    analysis: AppVisualAnalysis,
    observation_id: str,
    observed_at: datetime,
) -> None:
    """视觉事实晚于清单就绪时重新唤醒，禁止把已封存或旧会话重新发布。"""

    # Manifest 事件可能已经在 Vision 返回前被消费；该锁与 Index 封存共用，
    # 确保新唤醒只引用仍属于当前活会话和当前阶段的真实 PASS Manifest。
    await connection.execute(
        """
        SELECT pg_advisory_xact_lock(
            hashtext('app-evidence-index:' || capture.agent_run_id::text)
        )
        FROM qc_capture AS capture WHERE capture.id = $1::uuid
        """,
        analysis.capture_id,
    )
    row = await connection.fetchrow(
        f"""
        SELECT manifest.manifest_payload
        FROM qc_capture AS capture
        JOIN qc_task AS task ON task.id = capture.task_id
        JOIN qc_agent_run AS run
          ON run.id = capture.agent_run_id AND run.task_id = task.id
        JOIN qc_surface_session AS session
          ON session.session_id = capture.surface_session_id
         AND session.task_id = task.id
         AND session.surface_kind = capture.surface_kind
        JOIN LATERAL (
            SELECT candidate.manifest_payload, candidate.quality_status
            FROM qc_evidence_manifest AS candidate
            WHERE candidate.task_id = task.id
              AND candidate.session_id = session.session_id
              AND candidate.capture_id = capture.id
            ORDER BY candidate.manifest_version DESC LIMIT 1
        ) AS manifest ON manifest.quality_status = 'pass'
        WHERE capture.id = $1::uuid
          AND capture.capture_status = 'captured'
          AND capture.surface_kind IN ('android', 'ios')
          AND task.target_platform = 'app' AND task.status = 'running'
          AND task.task_config->>'workflow_version' = 'v2'
          AND task.task_config->>'execution_mode' = 'full_inspection'
          AND task.finalization_state IN ('open', 'exploration_draining', 'session_draining')
          AND run.status = 'running' AND session.status = 'active'
          AND session.finalization_epoch = task.finalization_epoch
          AND {capture_epoch_sql('capture')} = task.finalization_epoch
          AND ($2::boolean OR {startup_result_pair_candidate_sql()})
          AND NOT EXISTS (
              SELECT 1 FROM qc_app_evidence_index AS evidence_index
              WHERE evidence_index.task_id = task.id
                AND evidence_index.agent_run_id = run.id
                AND evidence_index.status = 'sealed'
          )
        FOR SHARE OF task, run, session
        """,
        analysis.capture_id,
        bool(analysis.visual_page_type),
    )
    if row is None:
        return
    manifest = EvidenceManifestReference.from_dict(
        _json_object(row["manifest_payload"], "manifest_payload")
    )
    if analysis.screenshot_artifact_id not in {item.artifact_id for item in manifest.artifacts}:
        raise AppVisualObservationPersistenceError("视觉唤醒的截图不属于可信 Manifest")
    event = build_evidence_bundle_ready(manifest, occurred_at=observed_at)
    await insert_outbox_event(
        connection,
        aggregate_type="app_visual_observation",
        aggregate_id=observation_id,
        envelope=build_protocol_envelope(
            message_type="alioth.v2.evidence-bundle.ready",
            payload_protocol_version=event.protocol_version,
            idempotency_key=f"app-visual-inspection-ready:{observation_id}:{analysis.result_hash}",
            task_id=manifest.task_id,
            aggregate_type="app_visual_observation",
            aggregate_id=observation_id,
            aggregate_version=1,
            occurred_at=observed_at,
            payload=event.to_dict(),
            correlation_id=manifest.task_id,
            causation_id=observation_id,
        ),
    )


def _analysis_from_row(row: Mapping[str, Any]) -> AppVisualAnalysis:
    try:
        return AppVisualAnalysis.from_dict(
            {
                "contract_version": str(row["contract_version"]),
                "capture_id": str(row["capture_id"]),
                "screenshot_artifact_id": str(row["screenshot_artifact_id"]),
                "image_signature": str(row["image_signature"]),
                "width": int(row["width"]),
                "height": int(row["height"]),
                "orientation": str(row["orientation"]),
                "package_name": row["package_name"],
                "activity": row["activity"],
                "visual_page_type": row["visual_page_type"],
                "visual_summary": str(row["visual_summary"]),
                "key_elements": _json_array(row["key_elements"], "key_elements"),
                "candidates": _json_array(row["candidate_targets"], "candidate_targets"),
                "page_state": row.get("page_state", "unknown"),
                "overlay_signature": row.get("overlay_signature"),
                "surface_facts": _json_object(
                    row.get("surface_facts", {}), "surface_facts"
                ),
                "provider_id": str(row["provider_id"]),
                "model_id": str(row["model_id"]),
                "result_hash": str(row["result_hash"]),
            }
        )
    except (AppVisualValidationError, TypeError, ValueError) as exc:
        raise AppVisualObservationPersistenceError("持久化 App 视觉事实合同无效") from exc


def _json_array(value: Any, field_name: str) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise AppVisualObservationPersistenceError(f"{field_name} 必须为 JSON 数组")
    return value


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise AppVisualObservationPersistenceError(f"{field_name} 必须为 JSON 对象")
    return dict(value)


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AppVisualObservationPersistenceError("视觉事实必须可 JSON 序列化") from exc
