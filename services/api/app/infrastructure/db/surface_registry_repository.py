"""逻辑 Surface Registry、Pin 与释放的 PostgreSQL 实现。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.domain.interaction import (
    SURFACE_REFERENCE_PROTOCOL_VERSION,
    SurfaceKind,
    SurfaceReference,
    SurfaceReferenceStatus,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.surface_session_repository import (
    SurfaceSessionPersistenceError,
)


class PostgresSurfaceRegistryRepository:
    """管理逻辑 Surface 身份，不持有 Page、Tab 或 Appium Driver 句柄。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def register_surface(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_ref_id: str,
        surface_key: str,
        surface_kind: SurfaceKind,
    ) -> SurfaceReference:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_surface_ref AS existing (
                    surface_ref_id, session_id, task_id, surface_kind,
                    surface_key, status
                )
                SELECT $3, session.session_id, session.task_id, $5, $4, 'active'
                FROM qc_surface_session AS session
                WHERE session.task_id = $1::uuid
                  AND session.session_id = $2
                  AND session.surface_kind = $5
                  AND session.status IN ('active', 'draining')
                ON CONFLICT (surface_ref_id) DO UPDATE
                SET surface_ref_id = existing.surface_ref_id
                WHERE existing.session_id = EXCLUDED.session_id
                  AND existing.task_id = EXCLUDED.task_id
                  AND existing.surface_kind = EXCLUDED.surface_kind
                  AND existing.surface_key = EXCLUDED.surface_key
                  AND existing.status NOT IN ('released', 'lost')
                RETURNING existing.*
                """,
                task_id,
                session_id,
                surface_ref_id,
                surface_key,
                surface_kind.value,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("Surface 归属无效或幂等身份漂移")
        return _parse_surface(row)

    async def transition_surface(
        self,
        *,
        task_id: str,
        surface_ref_id: str,
        expected_status: SurfaceReferenceStatus,
        target_status: SurfaceReferenceStatus,
        row_version: int,
        pin_reason: str | None = None,
        current_capture_id: str | None = None,
    ) -> SurfaceReference:
        _validate_surface_transition(expected_status, target_status, pin_reason)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_ref AS surface
                SET
                    status = $4,
                    pin_reason = CASE WHEN $4 = 'pinned' THEN $6 ELSE NULL END,
                    current_capture_id = COALESCE(capture.id, surface.current_capture_id),
                    released_at = CASE
                        WHEN $4 = ANY($7::varchar[]) THEN now()
                        ELSE NULL
                    END,
                    row_version = surface.row_version + 1,
                    updated_at = now()
                FROM (SELECT 1) AS singleton
                LEFT JOIN qc_capture AS capture
                    ON capture.id = $8::uuid
                   AND capture.task_id = $1::uuid
                WHERE surface.task_id = $1::uuid
                  AND surface.surface_ref_id = $2
                  AND surface.status = $3
                  AND surface.row_version = $5
                  AND (
                    $8::uuid IS NULL
                    OR (
                        capture.id IS NOT NULL
                        AND capture.surface_session_id = surface.session_id
                    )
                  )
                RETURNING surface.*
                """,
                task_id,
                surface_ref_id,
                expected_status.value,
                target_status.value,
                row_version,
                pin_reason,
                [
                    SurfaceReferenceStatus.RELEASED.value,
                    SurfaceReferenceStatus.LOST.value,
                ],
                current_capture_id,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("Surface 状态、版本或 Capture 归属已变化")
        return _parse_surface(row)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_surface(row: Mapping[str, Any]) -> SurfaceReference:
    return SurfaceReference(
        surface_ref_id=str(row["surface_ref_id"]),
        session_id=str(row["session_id"]),
        task_id=str(row["task_id"]),
        surface_kind=SurfaceKind(str(row["surface_kind"])),
        status=SurfaceReferenceStatus(str(row["status"])),
        surface_key=str(row["surface_key"]),
        current_capture_id=(
            str(row["current_capture_id"]) if row["current_capture_id"] is not None else None
        ),
        pin_reason=row["pin_reason"],
        row_version=int(row["row_version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        protocol_version=SURFACE_REFERENCE_PROTOCOL_VERSION,
    )


_SURFACE_TRANSITIONS = {
    SurfaceReferenceStatus.ACTIVE: {
        SurfaceReferenceStatus.PINNED,
        SurfaceReferenceStatus.DRAINING,
        SurfaceReferenceStatus.RELEASED,
        SurfaceReferenceStatus.LOST,
    },
    SurfaceReferenceStatus.PINNED: {
        SurfaceReferenceStatus.ACTIVE,
        SurfaceReferenceStatus.DRAINING,
        SurfaceReferenceStatus.RELEASED,
        SurfaceReferenceStatus.LOST,
    },
    SurfaceReferenceStatus.DRAINING: {
        SurfaceReferenceStatus.RELEASED,
        SurfaceReferenceStatus.LOST,
    },
}


def _validate_surface_transition(
    expected_status: SurfaceReferenceStatus,
    target_status: SurfaceReferenceStatus,
    pin_reason: str | None,
) -> None:
    if target_status not in _SURFACE_TRANSITIONS.get(expected_status, set()):
        raise ValueError("Surface 状态迁移不受支持")
    if target_status is SurfaceReferenceStatus.PINNED:
        _require_text(pin_reason, "pin_reason")
    elif pin_reason is not None:
        raise ValueError("只有 Pin 操作可以携带 pin_reason")


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
