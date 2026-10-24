"""Web 清单读取：主线先过滤可选引导 Capture，再解析清单。"""

from __future__ import annotations

import logging

from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
    _parse_capture_manifest,
)

_LOGGER = logging.getLogger(__name__)

_WEB_SESSION_MANIFEST_SQL = """
    WITH latest_manifest AS (
        SELECT DISTINCT ON (manifest.capture_id)
            manifest.capture_id, manifest.manifest_payload
        FROM qc_evidence_manifest AS manifest
        WHERE manifest.task_id = $1::uuid
        ORDER BY manifest.capture_id, manifest.manifest_version DESC
    )
    SELECT capture.id::text AS capture_id,
        latest_manifest.manifest_payload,
        capture.agent_run_id::text AS agent_run_id,
        capture.capture_role, capture.captured_at,
        capture.metadata, capture.action_execution_id
    FROM qc_capture AS capture
    LEFT JOIN latest_manifest ON latest_manifest.capture_id = capture.id
    WHERE capture.task_id = $1::uuid
      AND capture.surface_session_id = $2
      AND capture.agent_run_id = $3::uuid
      AND capture.capture_status = 'captured'
      AND {capture_purpose_predicate}
    ORDER BY capture.captured_at, capture.id
"""

_MAIN_PREDICATE = (
    "capture.metadata->>'capture_purpose' IS DISTINCT FROM 'web_guide_page'"
)
_GUIDE_PREDICATE = "capture.metadata->>'capture_purpose' = 'web_guide_page'"


class PostgresWebEvidenceManifestRepository(PostgresEvidenceManifestRepository):
    """普通 Capture 严格读取，引导 Capture 逐条宽容读取。"""

    async def list_latest_for_session(
        self, *, task_id: str, session_id: str, agent_run_id: str,
    ):
        rows = await self._list_web_capture_rows(
            task_id=task_id, session_id=session_id, agent_run_id=agent_run_id,
            capture_purpose_predicate=_MAIN_PREDICATE,
        )
        return tuple(_parse_capture_manifest(row) for row in rows)

    async def list_optional_guides_for_session(
        self, *, task_id: str, session_id: str, agent_run_id: str,
    ):
        rows = await self._list_web_capture_rows(
            task_id=task_id, session_id=session_id, agent_run_id=agent_run_id,
            capture_purpose_predicate=_GUIDE_PREDICATE,
        )
        guides = []
        for row in rows:
            try:
                guides.append(_parse_capture_manifest(row))
            except Exception:  # noqa: BLE001 - 损坏的可选引导清单不得中断主线
                _LOGGER.warning(
                    "web_guide_manifest_parse_failed capture_id=%s", row.get("capture_id"),
                )
        return tuple(guides)

    async def _list_web_capture_rows(
        self, *, task_id: str, session_id: str, agent_run_id: str,
        capture_purpose_predicate: str,
    ):
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await connection.fetch(
                _WEB_SESSION_MANIFEST_SQL.format(
                    capture_purpose_predicate=capture_purpose_predicate,
                ),
                task_id, session_id, agent_run_id,
            )
