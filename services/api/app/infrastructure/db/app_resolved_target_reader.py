"""从 PostgreSQL 按截图事实读取 App 视觉动作候选。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from services.api.app.domain.exploration import (
    AppVisualCandidate,
    AppVisualValidationError,
)
from services.api.app.domain.protocol import ProtocolContractError, require_text
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresAppResolvedTargetReader:
    """只从同一 Task/Session/Capture 的持久化视觉事实中解析候选。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def read_resolved_candidate(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        candidate_id: str,
        package_name: str,
    ) -> AppVisualCandidate | None:
        """读取与当前 App 截图身份完全一致的唯一候选。"""
        inputs = _validated_inputs(
            task_id=task_id,
            session_id=session_id,
            capture_id=capture_id,
            candidate_id=candidate_id,
            package_name=package_name,
        )
        if inputs is None:
            return None
        task_id, session_id, capture_id, candidate_id, package_name = inputs

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.task_id::text AS capture_task_id,
                    capture.surface_session_id AS capture_session_id,
                    capture.surface_kind AS capture_surface_kind,
                    capture.capture_status AS capture_status,
                    capture.id::text AS capture_id,
                    visual.task_id::text AS visual_task_id,
                    visual.session_id AS visual_session_id,
                    visual.capture_id::text AS visual_capture_id,
                    visual.screenshot_artifact_id::text
                        AS visual_screenshot_artifact_id,
                    app_capture.capture_id::text AS app_capture_id,
                    app_capture.screenshot_artifact_id::text
                        AS app_screenshot_artifact_id,
                    app_capture.image_signature,
                    app_capture.width,
                    app_capture.height,
                    app_capture.orientation,
                    app_capture.package_name,
                    visual.candidate_targets
                FROM qc_capture AS capture
                INNER JOIN qc_app_capture AS app_capture
                    ON app_capture.capture_id = capture.id
                INNER JOIN qc_app_visual_observation AS visual
                    ON visual.task_id = capture.task_id
                   AND visual.session_id = capture.surface_session_id
                   AND visual.capture_id = app_capture.capture_id
                   AND visual.screenshot_artifact_id = app_capture.screenshot_artifact_id
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.id = $3::uuid
                  AND capture.surface_kind = 'android'
                  AND capture.capture_status = 'captured'
                  AND app_capture.package_name = $5
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(visual.candidate_targets) AS candidate
                      WHERE candidate->>'candidate_id' = $4
                  )
                """,
                task_id,
                session_id,
                capture_id,
                candidate_id,
                package_name,
            )
        return _candidate_from_row(
            row,
            task_id=task_id,
            session_id=session_id,
            capture_id=capture_id,
            candidate_id=candidate_id,
            package_name=package_name,
        )

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


def _validated_inputs(
    *,
    task_id: Any,
    session_id: Any,
    capture_id: Any,
    candidate_id: Any,
    package_name: Any,
) -> tuple[str, str, str, str, str] | None:
    try:
        values = (
            require_text(task_id, "task_id"),
            require_text(session_id, "session_id"),
            require_text(capture_id, "capture_id"),
            require_text(candidate_id, "candidate_id"),
            require_text(package_name, "package_name"),
        )
        UUID(values[0])
        UUID(values[2])
    except (ProtocolContractError, TypeError, ValueError, AttributeError):
        return None
    return values


def _candidate_from_row(
    row: Mapping[str, Any] | None,
    *,
    task_id: str,
    session_id: str,
    capture_id: str,
    candidate_id: str,
    package_name: str,
) -> AppVisualCandidate | None:
    if row is None:
        return None
    try:
        if not _same_uuid(row["capture_task_id"], task_id):
            return None
        if row["capture_session_id"] != session_id:
            return None
        if row["capture_surface_kind"] != "android":
            return None
        if row["capture_status"] != "captured":
            return None
        if not _same_uuid(row["capture_id"], capture_id):
            return None
        if not _same_uuid(row["visual_task_id"], task_id):
            return None
        if row["visual_session_id"] != session_id:
            return None
        if not _same_uuid(row["visual_capture_id"], capture_id):
            return None
        if not _same_uuid(row["app_capture_id"], capture_id):
            return None
        visual_artifact = _required_text(row["visual_screenshot_artifact_id"])
        app_artifact = _required_text(row["app_screenshot_artifact_id"])
        if visual_artifact is None or app_artifact is None or visual_artifact != app_artifact:
            return None
        if _required_text(row["package_name"]) != package_name:
            return None
        image_signature = _required_text(row["image_signature"])
        width = _positive_int(row["width"])
        height = _positive_int(row["height"])
        orientation = _required_text(row["orientation"])
        if image_signature is None or width is None or height is None or orientation is None:
            return None

        raw_candidates = _json_array(row["candidate_targets"])
        if raw_candidates is None:
            return None
        candidates = tuple(AppVisualCandidate.from_dict(item) for item in raw_candidates)
        if len({item.candidate_id for item in candidates}) != len(candidates):
            return None
        matches = tuple(item for item in candidates if item.candidate_id == candidate_id)
        if len(matches) != 1:
            return None
        candidate = matches[0]
        if not _same_uuid(candidate.source_capture_id, capture_id):
            return None
        if candidate.source_image_signature != image_signature:
            return None
        if candidate.source_width != width:
            return None
        if candidate.source_height != height:
            return None
        if candidate.source_orientation != orientation:
            return None
        return candidate
    except (AppVisualValidationError, KeyError, TypeError, ValueError, AttributeError):
        return None


def _same_uuid(value: Any, expected: str) -> bool:
    try:
        return UUID(str(value)) == UUID(expected)
    except (AttributeError, TypeError, ValueError):
        return False


def _required_text(value: Any) -> str | None:
    try:
        return require_text(value, "row")
    except (ProtocolContractError, TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _json_array(value: Any) -> list[Any] | None:
    try:
        if isinstance(value, str):
            value = json.loads(value)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None
